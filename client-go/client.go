// Package cfclient 是 cf-slayer 服务的 Go 客户端。
//
// 职责边界很窄：向 Python 服务求解 cf_clearance，按域名缓存在内存里，
// 提前续期，然后把 cookie 与求解时的 User-Agent 套到 http.Request 上回放。
// 它不生成挑战令牌，也不处理代理。
//
// 与服务端的契约来自 solver/main.py 的响应体：
//
//	POST /solve  -> {clearance, user_agent, domain, expires_in, cached, solve_seconds, attempts}
//	POST /solve (want=turnstile) -> {want, token, user_agent, domain, solve_seconds, cached}
//	错误体       -> {"error": {code, message, attempts?}}
//
// 字段名必须与服务端完全一致；本包只用了客户端需要的子集。
//
// 硬性规则：同一出口 IP、同一 User-Agent、约 30 分钟时效、按域名隔离。
// 因此缓存按域名做键，回放时同时注入 cookie 与 UA。
package cfclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Credential 是一次求解的结果，字段标签与服务端 JSON 一致。
type Credential struct {
	Clearance string `json:"clearance"`
	UserAgent string `json:"user_agent"`
	Domain    string `json:"domain"`
	ExpiresIn int    `json:"expires_in"`
	Cached    bool   `json:"cached"`
}

// Token 是一次性 Turnstile 令牌。
//
// 令牌首次使用即被消费，不能缓存、不能续期，也不能换 User-Agent 使用；
// 因此本结构没有过期时间，Cached 恒为 false。消费方必须用 UserAgent 回放。
type Token struct {
	Token        string  `json:"token"`
	UserAgent    string  `json:"user_agent"`
	Domain       string  `json:"domain"`
	SolveSeconds float64 `json:"solve_seconds"`
	Cached       bool    `json:"cached"`
}

// Attempt 是服务端 attempts 数组中的一条。失败时用于诊断。
type Attempt struct {
	Backend string  `json:"backend"`
	OK      bool    `json:"ok"`
	Error   any     `json:"error"`
	Seconds float64 `json:"seconds"`
}

// APIError 是服务端返回的结构化错误。
type APIError struct {
	StatusCode int
	Code       string
	Message    string
	Attempts   []Attempt
}

func (e *APIError) Error() string {
	if e.Code == "" {
		return fmt.Sprintf("cf-slayer: HTTP %d: %s", e.StatusCode, e.Message)
	}
	return fmt.Sprintf("cf-slayer: %s: %s", e.Code, e.Message)
}

// solveRequest 是 POST /solve 的请求体。want 固定为 clearance。
type solveRequest struct {
	URL  string `json:"url"`
	Want string `json:"want"`
}

// turnstileRequest 是 want=turnstile 的请求体。
type turnstileRequest struct {
	URL     string `json:"url"`
	Want    string `json:"want"`
	Sitekey string `json:"sitekey"`
}

type turnstileResponse struct {
	Want         string  `json:"want"`
	Token        string  `json:"token"`
	UserAgent    string  `json:"user_agent"`
	Domain       string  `json:"domain"`
	SolveSeconds float64 `json:"solve_seconds"`
	Cached       bool    `json:"cached"`
}

type solveResponse struct {
	Credential
	SolveSeconds float64   `json:"solve_seconds"`
	Attempts     []Attempt `json:"attempts"`
}

type errorEnvelope struct {
	Error struct {
		Code     string    `json:"code"`
		Message  string    `json:"message"`
		Attempts []Attempt `json:"attempts"`
	} `json:"error"`
}

// domainEntry 是一个域名的内存状态。锁的顺序：先 e.mu，后不取 Client 的锁。
type domainEntry struct {
	mu        sync.Mutex
	cred      Credential
	expiresAt time.Time
	timer     *time.Timer
	gen       uint64
}

// Client 面向单个 cf-slayer 服务实例。
//
// 零值不可用；用 New 构造。HTTP 字段可替换（测试用），其余字段不建议改。
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
	// Logf 可选。本包只记录域名、状态码与错误，不记录凭据内容。
	Logf func(format string, args ...any)

	renewTimeout time.Duration
	now          func() time.Time
	cache        sync.Map // domain -> *domainEntry
}

// New 构造客户端。baseURL 形如 http://127.0.0.1:8878。
func New(baseURL, token string) (*Client, error) {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil {
		return nil, fmt.Errorf("cf-slayer: 服务地址无效: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("cf-slayer: 服务地址必须是 http(s)，收到 %q", baseURL)
	}
	if u.Host == "" {
		return nil, fmt.Errorf("cf-slayer: 服务地址缺少主机: %q", baseURL)
	}
	return &Client{
		BaseURL:      strings.TrimRight(u.String(), "/"),
		Token:        token,
		HTTP:         &http.Client{Timeout: 180 * time.Second},
		renewTimeout: 180 * time.Second,
		now:          time.Now,
	}, nil
}

func (c *Client) logf(format string, args ...any) {
	if c.Logf != nil {
		c.Logf(format, args...)
	}
}

// Solve 返回某 URL 所属域名的凭据。内存缓存有效时不发任何 HTTP 请求。
func (c *Client) Solve(ctx context.Context, rawURL string) (Credential, error) {
	domain, err := domainOf(rawURL)
	if err != nil {
		return Credential{}, err
	}
	e := c.entry(domain)
	e.mu.Lock()
	if e.valid(c.now()) {
		cred := e.cred
		cred.Cached = true
		cred.ExpiresIn = int(e.expiresAt.Sub(c.now()).Seconds())
		e.mu.Unlock()
		return cred, nil
	}
	e.mu.Unlock()

	cred, err := c.solveRemote(ctx, rawURL)
	if err != nil {
		return Credential{}, err
	}
	e.mu.Lock()
	e.cred = cred
	e.expiresAt = c.now().Add(time.Duration(cred.ExpiresIn) * time.Second)
	e.mu.Unlock()
	c.armRenewal(e, rawURL, cred.ExpiresIn)
	return cred, nil
}

// SolveTurnstile 请求一次性的 Turnstile 令牌。
//
// 与 Solve 不同：不读也不写内存缓存，不安排提前续期，每次调用都是一次
// 独立的 HTTP 请求。令牌由服务端现场求解，返回后立即交给调用方使用。
func (c *Client) SolveTurnstile(ctx context.Context, rawURL, sitekey string) (Token, error) {
	if _, err := domainOf(rawURL); err != nil {
		return Token{}, err
	}
	sitekey = strings.TrimSpace(sitekey)
	if sitekey == "" {
		return Token{}, errors.New("cf-slayer: sitekey 不能为空")
	}

	body, err := json.Marshal(turnstileRequest{
		URL:     rawURL,
		Want:    "turnstile",
		Sitekey: sitekey,
	})
	if err != nil {
		return Token{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/solve", bytes.NewReader(body))
	if err != nil {
		return Token{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Token{}, fmt.Errorf("cf-slayer: 请求令牌服务失败: %w", err)
	}
	defer closeBody(resp)

	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return Token{}, fmt.Errorf("cf-slayer: 读取令牌响应失败: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return Token{}, decodeAPIError(resp.StatusCode, payload)
	}
	var out turnstileResponse
	if err := json.Unmarshal(payload, &out); err != nil {
		return Token{}, fmt.Errorf("cf-slayer: 令牌响应不是合法 JSON: %w", err)
	}
	if out.Want != "turnstile" {
		return Token{}, fmt.Errorf("cf-slayer: 令牌响应的 want 异常: %q", out.Want)
	}
	if out.Token == "" || out.UserAgent == "" {
		return Token{}, errors.New("cf-slayer: 服务端返回了空令牌或空 User-Agent")
	}
	// 令牌不可缓存：即便服务端误报 cached=true，也强制归零。
	out.Cached = false
	return Token{
		Token:        out.Token,
		UserAgent:    out.UserAgent,
		Domain:       out.Domain,
		SolveSeconds: out.SolveSeconds,
		Cached:       out.Cached,
	}, nil
}

// Do 注入 cf_clearance 与求解时的 User-Agent 后执行请求。
//
// 遇到 403 时：作废该域缓存，重新求解一次，重试一次；仍是 403 就原样返回，
// 由调用方决定。绝不重试第二次。请求体不可重放时（Body 非空且没有 GetBody），
// 不重试，直接返回首个响应。
func (c *Client) Do(req *http.Request) (*http.Response, error) {
	if req == nil || req.URL == nil {
		return nil, errors.New("cf-slayer: 请求为空")
	}
	target := req.URL.String()
	cred, err := c.Solve(req.Context(), target)
	if err != nil {
		return nil, err
	}
	resp, err := c.send(req, cred)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusForbidden {
		return resp, nil
	}
	if req.Body != nil && req.GetBody == nil {
		c.logf("域名 %s 返回 403，但请求体不可重放；不重试", cred.Domain)
		return resp, nil
	}

	first := resp
	c.invalidate(cred.Domain)
	cred, err = c.Solve(req.Context(), target)
	if err != nil {
		closeBody(first)
		return nil, err
	}
	retry, err := cloneRequest(req)
	if err != nil {
		closeBody(first)
		return nil, err
	}
	closeBody(first)
	return c.send(retry, cred)
}

// send 复制请求、注入凭据并发出，不修改调用方的请求对象。
func (c *Client) send(req *http.Request, cred Credential) (*http.Response, error) {
	clone, err := cloneRequest(req)
	if err != nil {
		return nil, err
	}
	if cred.UserAgent != "" {
		clone.Header.Set("User-Agent", cred.UserAgent)
	}
	clone.Header.Set("Cookie", mergeCookie(clone.Header.Get("Cookie"), "cf_clearance", cred.Clearance))
	return c.HTTP.Do(clone)
}

func cloneRequest(req *http.Request) (*http.Request, error) {
	clone := req.Clone(req.Context())
	if req.Body != nil && req.GetBody != nil {
		body, err := req.GetBody()
		if err != nil {
			return nil, fmt.Errorf("cf-slayer: 无法重放请求体: %w", err)
		}
		clone.Body = body
	}
	return clone, nil
}

func closeBody(resp *http.Response) {
	if resp != nil && resp.Body != nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
		_ = resp.Body.Close()
	}
}

// mergeCookie 保留已有 cookie，替换同名项，并让 cf_clearance 排在首位。
func mergeCookie(existing, name, value string) string {
	pairs := make([]string, 0, 8)
	pairs = append(pairs, name+"="+value)
	for _, part := range strings.Split(existing, ";") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		if key, _, ok := strings.Cut(part, "="); ok && strings.TrimSpace(key) == name {
			continue
		}
		pairs = append(pairs, part)
	}
	return strings.Join(pairs, "; ")
}

func (c *Client) entry(domain string) *domainEntry {
	if v, ok := c.cache.Load(domain); ok {
		return v.(*domainEntry)
	}
	fresh := &domainEntry{}
	actual, _ := c.cache.LoadOrStore(domain, fresh)
	return actual.(*domainEntry)
}

func (e *domainEntry) valid(now time.Time) bool {
	return e.cred.Clearance != "" && now.Before(e.expiresAt)
}

func (c *Client) invalidate(domain string) {
	v, ok := c.cache.LoadAndDelete(domain)
	if !ok {
		return
	}
	e := v.(*domainEntry)
	e.mu.Lock()
	e.gen++
	if e.timer != nil {
		e.timer.Stop()
		e.timer = nil
	}
	e.cred = Credential{}
	e.expiresAt = time.Time{}
	e.mu.Unlock()
}

// armRenewal 在 TTL 的 80% 处安排一次后台续期。
func (c *Client) armRenewal(e *domainEntry, rawURL string, ttlSeconds int) {
	ttl := time.Duration(ttlSeconds) * time.Second
	delay := ttl * 8 / 10
	if delay < 10*time.Millisecond {
		delay = 10 * time.Millisecond
	}
	e.mu.Lock()
	if e.timer != nil {
		e.timer.Stop()
	}
	e.gen++
	gen := e.gen
	e.timer = time.AfterFunc(delay, func() { c.renew(e, rawURL, gen) })
	e.mu.Unlock()
}

func (c *Client) renew(e *domainEntry, rawURL string, gen uint64) {
	e.mu.Lock()
	if e.gen != gen {
		e.mu.Unlock()
		return
	}
	domain := e.cred.Domain
	e.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), c.renewTimeout)
	defer cancel()
	cred, err := c.solveRemote(ctx, rawURL)

	e.mu.Lock()
	defer e.mu.Unlock()
	if e.gen != gen {
		return
	}
	if err != nil {
		c.logf("域名 %s 提前续期失败：%v", domain, err)
		remaining := e.expiresAt.Sub(c.now())
		if remaining <= 0 {
			return
		}
		delay := remaining / 2
		if delay < time.Second {
			delay = time.Second
		}
		if delay > remaining {
			delay = remaining
		}
		e.timer = time.AfterFunc(delay, func() { c.renew(e, rawURL, gen) })
		return
	}
	e.cred = cred
	e.expiresAt = c.now().Add(time.Duration(cred.ExpiresIn) * time.Second)
	if e.timer != nil {
		e.timer.Stop()
	}
	ttl := time.Duration(cred.ExpiresIn) * time.Second
	delay := ttl * 8 / 10
	if delay < 10*time.Millisecond {
		delay = 10 * time.Millisecond
	}
	e.gen++
	next := e.gen
	e.timer = time.AfterFunc(delay, func() { c.renew(e, rawURL, next) })
}

func (c *Client) solveRemote(ctx context.Context, rawURL string) (Credential, error) {
	if _, err := domainOf(rawURL); err != nil {
		return Credential{}, err
	}
	body, err := json.Marshal(solveRequest{URL: rawURL, Want: "clearance"})
	if err != nil {
		return Credential{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/solve", bytes.NewReader(body))
	if err != nil {
		return Credential{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Credential{}, fmt.Errorf("cf-slayer: 请求求解服务失败: %w", err)
	}
	defer closeBody(resp)

	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return Credential{}, fmt.Errorf("cf-slayer: 读取响应失败: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return Credential{}, decodeAPIError(resp.StatusCode, payload)
	}
	var out solveResponse
	if err := json.Unmarshal(payload, &out); err != nil {
		return Credential{}, fmt.Errorf("cf-slayer: 响应不是合法 JSON: %w", err)
	}
	if out.Clearance == "" || out.UserAgent == "" {
		return Credential{}, errors.New("cf-slayer: 服务端返回了空凭据或空 User-Agent")
	}
	if out.ExpiresIn <= 0 {
		return Credential{}, errors.New("cf-slayer: 服务端返回的 expires_in 非正")
	}
	return out.Credential, nil
}

func decodeAPIError(status int, payload []byte) error {
	var env errorEnvelope
	apiErr := &APIError{StatusCode: status}
	if err := json.Unmarshal(payload, &env); err == nil && env.Error.Code != "" {
		apiErr.Code = env.Error.Code
		apiErr.Message = env.Error.Message
		apiErr.Attempts = env.Error.Attempts
		return apiErr
	}
	apiErr.Message = strings.TrimSpace(string(payload))
	if apiErr.Message == "" {
		apiErr.Message = http.StatusText(status)
	}
	return apiErr
}

// domainOf 返回小写主机名，与服务端的缓存键一致。
func domainOf(rawURL string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return "", fmt.Errorf("cf-slayer: 网址无效: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" || u.Hostname() == "" {
		return "", fmt.Errorf("cf-slayer: 网址必须是绝对的 http(s) 网址: %q", rawURL)
	}
	return strings.ToLower(u.Hostname()), nil
}
