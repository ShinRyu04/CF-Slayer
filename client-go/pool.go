package cfclient

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// worker 状态取值，出现在 Pool.Status() 中。
const (
	StateHealthy = "healthy"
	StateDown    = "down"
)

const (
	// 连续失败达到该次数即从哈希环摘除。
	failThreshold = 3
	// 健康探针连续成功该次数后回归哈希环。
	probesToRecover = 2
	// 每个 worker 的虚拟节点数。一致性哈希的常规取值，不需要调。
	virtualNodes = 128
)

// ErrNoWorkers 表示没有可用的 worker。调用方不要重试到天荒地老。
var ErrNoWorkers = errors.New("cf-slayer: 所有 worker 均不可用")

// WorkerStatus 是池中单个 worker 的可见状态。
type WorkerStatus struct {
	URL              string
	State            string
	ConsecutiveFails int
}

type worker struct {
	url    string
	client *Client
	state  string
	fails  int
	probes int
}

type ringNode struct {
	hash uint32
	idx  int
}

// Pool 把 N 个 cf-slayer 服务实例当成一个按域名粘性的整体。
//
// 同一域名稳定映射到同一 worker（一致性哈希），因此服务端的按域缓存才能真正命中。
// 连续失败的 worker 会被摘除，后台探针确认恢复后自动回归。
type Pool struct {
	token         string
	probeInterval time.Duration

	mu      sync.RWMutex
	workers []*worker
	ring    []ringNode

	probeClient *http.Client
	stop        chan struct{}
	done        chan struct{}
	closeOnce   sync.Once
}

// PoolOption 调整池行为，主要供测试缩短探针间隔。
type PoolOption func(*Pool)

// WithProbeInterval 设置健康探针间隔（默认 5 秒）。
func WithProbeInterval(d time.Duration) PoolOption {
	return func(p *Pool) { p.probeInterval = d }
}

// NewPool 用显式服务地址列表构造池并启动健康探针。
func NewPool(services []string, token string, opts ...PoolOption) (*Pool, error) {
	cleaned := make([]string, 0, len(services))
	seen := make(map[string]bool, len(services))
	for _, raw := range services {
		trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
		if trimmed == "" {
			continue
		}
		if seen[trimmed] {
			continue
		}
		seen[trimmed] = true
		cleaned = append(cleaned, trimmed)
	}
	if len(cleaned) == 0 {
		return nil, errors.New("cf-slayer: 没有配置任何服务地址")
	}

	p := &Pool{
		token:         token,
		probeInterval: 5 * time.Second,
		probeClient:   &http.Client{Timeout: 3 * time.Second},
		stop:          make(chan struct{}),
		done:          make(chan struct{}),
	}
	for _, opt := range opts {
		opt(p)
	}
	if p.probeInterval <= 0 {
		p.probeInterval = 5 * time.Second
	}

	p.mu.Lock()
	for _, serviceURL := range cleaned {
		client, err := New(serviceURL, token)
		if err != nil {
			p.mu.Unlock()
			return nil, err
		}
		p.workers = append(p.workers, &worker{url: serviceURL, client: client, state: StateHealthy})
	}
	p.rebuildRingLocked()
	p.mu.Unlock()

	go p.probeLoop()
	return p, nil
}

// NewPoolFromEnv 读取 CF_SLAYER_SERVICES（逗号分隔）与 CF_SLAYER_AUTH_TOKEN。
// 未设置服务列表时退化为单实例 http://127.0.0.1:8878。
func NewPoolFromEnv(opts ...PoolOption) (*Pool, error) {
	raw := strings.TrimSpace(os.Getenv("CF_SLAYER_SERVICES"))
	services := []string{"http://127.0.0.1:8878"}
	if raw != "" {
		services = strings.Split(raw, ",")
	}
	return NewPool(services, os.Getenv("CF_SLAYER_AUTH_TOKEN"), opts...)
}

// Solve 按域名选 worker 求解，失败时换下一个可用 worker（有界重试）。
func (p *Pool) Solve(ctx context.Context, rawURL string) (Credential, error) {
	domain, err := domainOf(rawURL)
	if err != nil {
		return Credential{}, err
	}
	candidates := p.candidates(domain)
	if len(candidates) == 0 {
		return Credential{}, ErrNoWorkers
	}
	var lastErr error
	for _, w := range candidates {
		cred, err := w.client.Solve(ctx, rawURL)
		if err == nil {
			p.recordSuccess(w.url)
			return cred, nil
		}
		if !isWorkerFailure(err) {
			return Credential{}, err
		}
		p.recordFailure(w.url)
		lastErr = err
	}
	return Credential{}, fmt.Errorf("%w（最后一次错误：%v）", ErrNoWorkers, lastErr)
}

// Do 用粘住的 worker 回放请求。传输层失败会换 worker 重试；目标站的 HTTP 状态
// 不算 worker 故障。
func (p *Pool) Do(req *http.Request) (*http.Response, error) {
	if req == nil || req.URL == nil {
		return nil, errors.New("cf-slayer: 请求为空")
	}
	domain, err := domainOf(req.URL.String())
	if err != nil {
		return nil, err
	}
	candidates := p.candidates(domain)
	if len(candidates) == 0 {
		return nil, ErrNoWorkers
	}
	var lastErr error
	for _, w := range candidates {
		resp, err := w.client.Do(req)
		if err == nil {
			// 回放直接打目标站，不经过 worker；成功不能证明 worker 还活着，
			// 所以这里不重置失败计数，交给健康探针判断。
			return resp, nil
		}
		if !isWorkerFailure(err) {
			return nil, err
		}
		p.recordFailure(w.url)
		lastErr = err
	}
	return nil, fmt.Errorf("%w（最后一次错误：%v）", ErrNoWorkers, lastErr)
}

// Status 返回所有 worker 的当前状态，顺序与构造时一致。
func (p *Pool) Status() []WorkerStatus {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make([]WorkerStatus, len(p.workers))
	for i, w := range p.workers {
		out[i] = WorkerStatus{URL: w.url, State: w.state, ConsecutiveFails: w.fails}
	}
	return out
}

// Close 停止探针并关闭空闲连接。
func (p *Pool) Close() {
	p.closeOnce.Do(func() {
		close(p.stop)
		<-p.done
		p.mu.RLock()
		defer p.mu.RUnlock()
		for _, w := range p.workers {
			w.client.HTTP.CloseIdleConnections()
		}
	})
}

func (p *Pool) candidates(domain string) []*worker {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make([]*worker, 0, len(p.workers))
	seen := make(map[int]bool, len(p.workers))
	if len(p.ring) > 0 {
		start := p.ringStartLocked(domain)
		for i := range p.ring {
			idx := p.ring[(start+i)%len(p.ring)].idx
			if seen[idx] {
				continue
			}
			seen[idx] = true
			out = append(out, p.workers[idx])
		}
	}
	for idx, w := range p.workers {
		if w.state != StateHealthy || seen[idx] {
			continue
		}
		out = append(out, w)
	}
	return out
}

func (p *Pool) pickLocked(domain string) *worker {
	if len(p.ring) == 0 {
		return nil
	}
	return p.workers[p.ring[p.ringStartLocked(domain)].idx]
}

func (p *Pool) ringStartLocked(domain string) int {
	h := fnv32a(domain)
	i := sort.Search(len(p.ring), func(i int) bool { return p.ring[i].hash >= h })
	if i == len(p.ring) {
		i = 0
	}
	return i
}

func (p *Pool) rebuildRingLocked() {
	ring := make([]ringNode, 0, len(p.workers)*virtualNodes)
	for idx, w := range p.workers {
		if w.state != StateHealthy {
			continue
		}
		for v := range virtualNodes {
			key := fmt.Sprintf("%s#%d", w.url, v)
			ring = append(ring, ringNode{hash: fnv32a(key), idx: idx})
		}
	}
	sort.Slice(ring, func(i, j int) bool {
		if ring[i].hash != ring[j].hash {
			return ring[i].hash < ring[j].hash
		}
		return ring[i].idx < ring[j].idx
	})
	p.ring = ring
}

func (p *Pool) recordFailure(serviceURL string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, w := range p.workers {
		if w.url != serviceURL || w.state != StateHealthy {
			continue
		}
		w.fails++
		if w.fails >= failThreshold {
			w.state = StateDown
			w.probes = 0
			p.rebuildRingLocked()
		}
		return
	}
}

func (p *Pool) recordSuccess(serviceURL string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, w := range p.workers {
		if w.url == serviceURL && w.state == StateHealthy {
			w.fails = 0
			return
		}
	}
}

func (p *Pool) probeLoop() {
	defer close(p.done)
	ticker := time.NewTicker(p.probeInterval)
	defer ticker.Stop()
	for {
		select {
		case <-p.stop:
			return
		case <-ticker.C:
			p.probeOnce()
		}
	}
}

// probeOnce 探测所有 worker。健康 worker 连续探活失败也计入失败次数，
// 这样进程被直接杀掉（连接拒绝）时无需等请求流量就能摘除。
func (p *Pool) probeOnce() {
	p.mu.RLock()
	urls := make([]string, len(p.workers))
	for i, w := range p.workers {
		urls[i] = w.url
	}
	p.mu.RUnlock()

	for _, serviceURL := range urls {
		if p.probeHealth(serviceURL) {
			p.markProbeSuccess(serviceURL)
		} else {
			p.markProbeFailure(serviceURL)
		}
	}
}

func (p *Pool) probeHealth(serviceURL string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, serviceURL+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := p.probeClient.Do(req)
	if err != nil {
		return false
	}
	defer closeBody(resp)
	return resp.StatusCode == http.StatusOK
}

func (p *Pool) markProbeSuccess(serviceURL string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, w := range p.workers {
		if w.url != serviceURL {
			continue
		}
		if w.state == StateHealthy {
			w.fails = 0
			return
		}
		w.probes++
		if w.probes >= probesToRecover {
			w.state = StateHealthy
			w.fails = 0
			w.probes = 0
			p.rebuildRingLocked()
		}
		return
	}
}

func (p *Pool) markProbeFailure(serviceURL string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, w := range p.workers {
		if w.url != serviceURL {
			continue
		}
		if w.state == StateDown {
			w.probes = 0
			return
		}
		w.fails++
		if w.fails >= failThreshold {
			w.state = StateDown
			w.probes = 0
			p.rebuildRingLocked()
		}
		return
	}
}

// isWorkerFailure 判断错误是否该归咎于 worker（可换下一个）。
// 4xx 是请求/鉴权问题，换 worker 也没用，直接上报。
func isWorkerFailure(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		// 调用方自己的截止时间不该算到 worker 头上。
		return false
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		return apiErr.StatusCode >= 500
	}
	return true
}

func fnv32a(s string) uint32 {
	const (
		offset32 = 2166136261
		prime32  = 16777619
	)
	h := uint32(offset32)
	for i := range s {
		h ^= uint32(s[i])
		h *= prime32
	}
	return h
}
