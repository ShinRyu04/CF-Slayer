package cfclient

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// 假服务：实现 /solve 与 /health，记录调用次数与收到的请求头。
type fakeService struct {
	t          *testing.T
	mu         sync.Mutex
	solveCalls int
	authSeen   []string
	expiresIn  int
	failStatus int
	delay      time.Duration
}

func newFakeService(t *testing.T, expiresIn int) (*fakeService, *httptest.Server) {
	t.Helper()
	fs := &fakeService{t: t, expiresIn: expiresIn}
	mux := http.NewServeMux()
	mux.HandleFunc("/solve", fs.handleSolve)
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"state":"ready","browser":"camoufox"}`))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return fs, srv
}

func (f *fakeService) handleSolve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		URL  string `json:"url"`
		Want string `json:"want"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Want != "clearance" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	f.mu.Lock()
	f.solveCalls++
	n := f.solveCalls
	auth := r.Header.Get("Authorization")
	f.authSeen = append(f.authSeen, auth)
	expires := f.expiresIn
	status := f.failStatus
	delay := f.delay
	f.mu.Unlock()

	if delay > 0 {
		time.Sleep(delay)
	}
	if status != 0 {
		w.WriteHeader(status)
		fmt.Fprintf(w, `{"error":{"code":"SOLVE_FAILED","message":"假服务故障"}}`)
		return
	}
	payload := map[string]any{
		"clearance":     fmt.Sprintf("tok-%d", n),
		"user_agent":    fmt.Sprintf("UA-%d", n),
		"domain":        "127.0.0.1",
		"expires_in":    expires,
		"cached":        false,
		"solve_seconds": 0.01,
		"attempts":      []map[string]any{},
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(payload)
}

func (f *fakeService) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.solveCalls
}

func (f *fakeService) setFailStatus(status int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failStatus = status
}

func mustClient(t *testing.T, baseURL, token string) *Client {
	t.Helper()
	c, err := New(baseURL, token)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func TestSolveCacheHitMakesNoHTTPCall(t *testing.T) {
	fs, srv := newFakeService(t, 60)
	c := mustClient(t, srv.URL, "secret-token")

	first, err := c.Solve(context.Background(), "https://target.example/a")
	if err != nil {
		t.Fatalf("首次 Solve: %v", err)
	}
	if first.Cached {
		t.Fatal("首次求解不应标记为 cached")
	}
	second, err := c.Solve(context.Background(), "https://target.example/b")
	if err != nil {
		t.Fatalf("第二次 Solve: %v", err)
	}
	if !second.Cached {
		t.Fatal("内存命中应标记 cached=true")
	}
	if second.Clearance != first.Clearance {
		t.Fatalf("命中返回了不同凭据: %q != %q", second.Clearance, first.Clearance)
	}
	if got := fs.calls(); got != 1 {
		t.Fatalf("命中缓存时服务端应只被调用 1 次，实际 %d", got)
	}
	if got := fs.authSeen[0]; got != "Bearer secret-token" {
		t.Fatalf("Authorization 头不对: %q", got)
	}
}

func TestSolveRejectsBadURL(t *testing.T) {
	fs, srv := newFakeService(t, 60)
	c := mustClient(t, srv.URL, "t")
	if _, err := c.Solve(context.Background(), "ftp://target.example/"); err == nil {
		t.Fatal("非 http(s) 网址应报错")
	}
	if fs.calls() != 0 {
		t.Fatal("坏网址不应触发 HTTP 调用")
	}
}

func TestEarlyRenewalHappensInBackground(t *testing.T) {
	fs, srv := newFakeService(t, 1) // 80% => 800ms 后续期
	c := mustClient(t, srv.URL, "t")

	if _, err := c.Solve(context.Background(), "https://target.example/"); err != nil {
		t.Fatalf("Solve: %v", err)
	}
	deadline := time.Now().Add(4 * time.Second)
	for fs.calls() < 2 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if got := fs.calls(); got < 2 {
		t.Fatalf("应在 TTL 80%% 处后台续期，实际调用 %d 次", got)
	}

	cred, err := c.Solve(context.Background(), "https://target.example/")
	if err != nil {
		t.Fatalf("续期后 Solve: %v", err)
	}
	if cred.Clearance != "tok-2" {
		t.Fatalf("续期后应使用新凭据，实际 %q", cred.Clearance)
	}
	if got := fs.calls(); got != 2 {
		t.Fatalf("续期后不应再有额外调用，实际 %d", got)
	}
}

func TestDoSelfHealsOn403ExactlyOnce(t *testing.T) {
	fs, svc := newFakeService(t, 60)
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetCalls.Add(1)
		cookie := r.Header.Get("Cookie")
		if strings.Contains(cookie, "cf_clearance=tok-2") {
			_, _ = w.Write([]byte("ok"))
			return
		}
		w.WriteHeader(http.StatusForbidden)
	}))
	t.Cleanup(target.Close)

	c := mustClient(t, svc.URL, "t")
	req, err := http.NewRequest(http.MethodGet, target.URL+"/page", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := c.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("自愈后应 200，实际 %d", resp.StatusCode)
	}
	if got := targetCalls.Load(); got != 2 {
		t.Fatalf("应恰好请求目标 2 次，实际 %d", got)
	}
	if got := fs.calls(); got != 2 {
		t.Fatalf("应恰好重新求解 1 次（共 2 次），实际 %d", got)
	}
}

func TestDoReturns403AfterSingleRetry(t *testing.T) {
	fs, svc := newFakeService(t, 60)
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetCalls.Add(1)
		w.WriteHeader(http.StatusForbidden)
	}))
	t.Cleanup(target.Close)

	c := mustClient(t, svc.URL, "t")
	req, _ := http.NewRequest(http.MethodGet, target.URL+"/page", nil)
	resp, err := c.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("重试后仍 403 应原样返回，实际 %d", resp.StatusCode)
	}
	if got := targetCalls.Load(); got != 2 {
		t.Fatalf("绝不重试第二次：目标应被请求 2 次，实际 %d", got)
	}
	if got := fs.calls(); got != 2 {
		t.Fatalf("应恰好重新求解 1 次（共 2 次），实际 %d", got)
	}
}

func TestDoInjectsUAAndPreservesOtherCookies(t *testing.T) {
	_, svc := newFakeService(t, 60)
	var gotUA, gotCookie string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		gotCookie = r.Header.Get("Cookie")
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(target.Close)

	c := mustClient(t, svc.URL, "t")
	req, _ := http.NewRequest(http.MethodGet, target.URL+"/", nil)
	req.Header.Set("Cookie", "session=abc; cf_clearance=stale")
	resp, err := c.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	if gotUA != "UA-1" {
		t.Fatalf("未注入求解时的 UA，实际 %q", gotUA)
	}
	if !strings.HasPrefix(gotCookie, "cf_clearance=tok-1; ") {
		t.Fatalf("cf_clearance 应在首位，实际 %q", gotCookie)
	}
	if !strings.Contains(gotCookie, "session=abc") {
		t.Fatalf("其他 cookie 应保留，实际 %q", gotCookie)
	}
	if strings.Count(gotCookie, "cf_clearance=") != 1 {
		t.Fatalf("cf_clearance 不应重复，实际 %q", gotCookie)
	}
}

func TestCredentialNeverAppearsInLogs(t *testing.T) {
	_, svc := newFakeService(t, 60)
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	t.Cleanup(target.Close)

	var mu sync.Mutex
	var logs []string
	c := mustClient(t, svc.URL, "t")
	c.Logf = func(format string, args ...any) {
		mu.Lock()
		defer mu.Unlock()
		logs = append(logs, fmt.Sprintf(format, args...))
	}

	req, _ := http.NewRequest(http.MethodGet, target.URL+"/", nil)
	resp, err := c.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	mu.Lock()
	defer mu.Unlock()
	joined := strings.Join(logs, "\n")
	if strings.Contains(joined, "tok-1") || strings.Contains(joined, "tok-2") {
		t.Fatalf("日志泄露了凭据: %q", joined)
	}
	if strings.Contains(joined, "UA-1") {
		t.Fatalf("日志泄露了 User-Agent: %q", joined)
	}
}

func TestSolveMapsAPIError(t *testing.T) {
	fs, srv := newFakeService(t, 60)
	fs.setFailStatus(http.StatusBadGateway)
	c := mustClient(t, srv.URL, "t")

	_, err := c.Solve(context.Background(), "https://target.example/")
	if err == nil {
		t.Fatal("应返回错误")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("错误类型应为 *APIError，实际 %T", err)
	}
	if apiErr.StatusCode != http.StatusBadGateway || apiErr.Code != "SOLVE_FAILED" {
		t.Fatalf("错误映射不对: %+v", apiErr)
	}
	if !isWorkerFailure(err) {
		t.Fatal("5xx 应被视为 worker 故障")
	}
}
