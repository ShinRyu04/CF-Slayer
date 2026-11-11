package cfclient

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type fakeWorker struct {
	url      string
	server   *httptest.Server
	solves   atomic.Int32
	fail     atomic.Bool
	healthOK atomic.Bool
}

func newFakeWorker(t *testing.T) *fakeWorker {
	t.Helper()
	fw := &fakeWorker{}
	fw.healthOK.Store(true)
	mux := http.NewServeMux()
	mux.HandleFunc("/solve", func(w http.ResponseWriter, r *http.Request) {
		n := fw.solves.Add(1)
		if fw.fail.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte(`{"error":{"code":"SOLVE_FAILED","message":"坏掉了"}}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"clearance":"w-%d","user_agent":"UA","domain":"127.0.0.1","expires_in":60,"cached":false,"solve_seconds":0.01,"attempts":[]}`, n)
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if !fw.healthOK.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"state":"ready"}`))
	})
	fw.server = httptest.NewServer(mux)
	fw.url = fw.server.URL
	t.Cleanup(fw.server.Close)
	return fw
}

func domainMappedTo(p *Pool, workerURL string) string {
	for i := 0; i < 100000; i++ {
		domain := fmt.Sprintf("d%d.example", i)
		if picked := p.pickLocked(domain); picked != nil && picked.url == workerURL {
			return domain
		}
	}
	panic("hash ring 中找不到该 worker")
}

func TestPoolHashStability(t *testing.T) {
	workers := []*fakeWorker{newFakeWorker(t), newFakeWorker(t), newFakeWorker(t)}
	urls := []string{workers[0].url, workers[1].url, workers[2].url}
	p, err := NewPool(urls, "t")
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	p.mu.RLock()
	defer p.mu.RUnlock()
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		domain := fmt.Sprintf("stable%d.example", i)
		first := p.pickLocked(domain)
		for j := 0; j < 5; j++ {
			if again := p.pickLocked(domain); again != first {
				t.Fatalf("域名 %s 的映射不稳定: %q -> %q", domain, first.url, again.url)
			}
		}
		seen[first.url] = true
	}
	if len(seen) != 3 {
		t.Fatalf("200 个域名应覆盖 3 个 worker，实际 %d", len(seen))
	}
}

func TestPoolFailoverAndRemoval(t *testing.T) {
	bad := newFakeWorker(t)
	good := newFakeWorker(t)
	bad.fail.Store(true)
	p, err := NewPool([]string{bad.url, good.url}, "t")
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	domain := domainMappedTo(p, bad.url)
	for i := 0; i < failThreshold; i++ {
		cred, err := p.Solve(context.Background(), "https://"+domain+"/p")
		if err != nil {
			t.Fatalf("第 %d 次应回退成功: %v", i+1, err)
		}
		if cred.Clearance == "" {
			t.Fatalf("第 %d 次回退返回空凭据", i+1)
		}
	}

	status := p.Status()
	if status[0].URL != bad.url || status[0].State != StateDown {
		t.Fatalf("连续 %d 次失败后应摘除，实际 %+v", failThreshold, status[0])
	}
	if status[0].ConsecutiveFails != failThreshold {
		t.Fatalf("失败计数应为 %d，实际 %d", failThreshold, status[0].ConsecutiveFails)
	}
	if status[1].State != StateHealthy {
		t.Fatalf("健康 worker 不应被摘除: %+v", status[1])
	}
	if got := bad.solves.Load(); got != failThreshold {
		t.Fatalf("坏 worker 应只被调用 %d 次，实际 %d", failThreshold, got)
	}
}

func TestPoolAllWorkersDown(t *testing.T) {
	a := newFakeWorker(t)
	b := newFakeWorker(t)
	a.fail.Store(true)
	b.fail.Store(true)
	p, err := NewPool([]string{a.url, b.url}, "t")
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	for i := 0; i < failThreshold; i++ {
		if _, err := p.Solve(context.Background(), fmt.Sprintf("https://down%d.example/", i)); !errors.Is(err, ErrNoWorkers) {
			t.Fatalf("第 %d 次应返回 ErrNoWorkers，实际 %v", i+1, err)
		}
	}
	for _, st := range p.Status() {
		if st.State != StateDown {
			t.Fatalf("所有 worker 都应被摘除: %+v", st)
		}
	}
	if _, err := p.Solve(context.Background(), "https://down.example/"); !errors.Is(err, ErrNoWorkers) {
		t.Fatalf("全挂后应立刻返回 ErrNoWorkers，实际 %v", err)
	}
}

func TestPoolRecoversAfterProbes(t *testing.T) {
	a := newFakeWorker(t)
	b := newFakeWorker(t)
	a.fail.Store(true)
	b.fail.Store(true)
	p, err := NewPool([]string{a.url, b.url}, "t", WithProbeInterval(20*time.Millisecond))
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	for i := 0; i < failThreshold; i++ {
		_, _ = p.Solve(context.Background(), fmt.Sprintf("https://down%d.example/", i))
	}
	a.fail.Store(false)
	b.fail.Store(false)

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		healthy := 0
		for _, st := range p.Status() {
			if st.State == StateHealthy {
				healthy++
			}
		}
		if healthy == 2 {
			cred, err := p.Solve(context.Background(), "https://recovered.example/")
			if err != nil {
				t.Fatalf("回归后 Solve: %v", err)
			}
			if cred.Clearance == "" {
				t.Fatal("回归后返回空凭据")
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("两次健康探针后应回归，实际状态 %+v", p.Status())
}

func TestPoolSingleWorkerCachesLocally(t *testing.T) {
	w := newFakeWorker(t)
	p, err := NewPool([]string{w.url}, "t")
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	if _, err := p.Solve(context.Background(), "https://single.example/"); err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if _, err := p.Solve(context.Background(), "https://single.example/"); err != nil {
		t.Fatalf("第二次 Solve: %v", err)
	}
	if got := w.solves.Load(); got != 1 {
		t.Fatalf("单 worker 下本地缓存应生效，服务端调用 %d 次", got)
	}
}

func TestPoolRejectsEmptyServices(t *testing.T) {
	if _, err := NewPool([]string{" ", ""}, "t"); err == nil || !strings.Contains(err.Error(), "没有配置") {
		t.Fatalf("空服务列表应报错，实际 %v", err)
	}
}

func TestPoolProbeRemovesSilentlyDeadWorker(t *testing.T) {
	w := newFakeWorker(t)
	other := newFakeWorker(t)
	p, err := NewPool([]string{w.url, other.url}, "t", WithProbeInterval(20*time.Millisecond))
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	defer p.Close()

	w.healthOK.Store(false)
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if p.Status()[0].State == StateDown {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if got := p.Status()[0]; got.State != StateDown || got.ConsecutiveFails < failThreshold {
		t.Fatalf("探活连续失败后应摘除，实际 %+v", got)
	}

	w.healthOK.Store(true)
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if p.Status()[0].State == StateHealthy {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("恢复探活后应回归，实际 %+v", p.Status()[0])
}
