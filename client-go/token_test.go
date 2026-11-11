package cfclient

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

// 令牌假服务：只接受 want=turnstile，记录 sitekey 与鉴权头。
type tokenService struct {
	mu        sync.Mutex
	calls     int
	sitekeys  []string
	wants     []string
	authSeen  []string
	status    int
	tokenBody string
}

func newTokenService(t *testing.T) (*tokenService, *httptest.Server) {
	t.Helper()
	ts := &tokenService{}
	mux := http.NewServeMux()
	mux.HandleFunc("/solve", func(w http.ResponseWriter, r *http.Request) {
		var body turnstileRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		ts.mu.Lock()
		ts.calls++
		n := ts.calls
		ts.sitekeys = append(ts.sitekeys, body.Sitekey)
		ts.wants = append(ts.wants, body.Want)
		ts.authSeen = append(ts.authSeen, r.Header.Get("Authorization"))
		status := ts.status
		ts.mu.Unlock()

		if status != 0 {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":{"code":"BAD_REQUEST","message":"缺少 sitekey","attempts":[{"backend":"camoufox","ok":false,"error":"不支持","seconds":0}]}}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"want":          "turnstile",
			"token":         "0." + string(rune('a'+n)),
			"user_agent":    "Mozilla/5.0 test",
			"domain":        "target.example",
			"solve_seconds": 0.25,
			"cached":        false,
		})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return ts, srv
}

func (ts *tokenService) snapshot() (int, []string, []string) {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	return ts.calls, append([]string(nil), ts.sitekeys...), append([]string(nil), ts.authSeen...)
}

func TestSolveTurnstile传站点密钥且不缓存(t *testing.T) {
	ts, srv := newTokenService(t)
	c, err := New(srv.URL, "test-token")
	if err != nil {
		t.Fatal(err)
	}

	首个, err := c.SolveTurnstile(context.Background(), "https://target.example/login", "0xAAAA")
	if err != nil {
		t.Fatalf("首次求解失败: %v", err)
	}
	次个, err := c.SolveTurnstile(context.Background(), "https://target.example/login", "0xBBBB")
	if err != nil {
		t.Fatalf("第二次求解失败: %v", err)
	}

	calls, sitekeys, authSeen := ts.snapshot()
	if calls != 2 {
		t.Fatalf("令牌必须每次重新求解，期望 2 次请求，得到 %d", calls)
	}
	if 首个.Token == 次个.Token {
		t.Fatalf("两次令牌不应相同: %q", 首个.Token)
	}
	if 首个.Domain != "target.example" || 首个.UserAgent != "Mozilla/5.0 test" {
		t.Fatalf("响应字段解析错误: %+v", 首个)
	}
	if 首个.Cached {
		t.Fatal("令牌的 cached 必须为 false")
	}
	for i, want := range []string{"0xAAAA", "0xBBBB"} {
		if sitekeys[i] != want {
			t.Fatalf("sitekey 未透传: 第 %d 次为 %q", i+1, sitekeys[i])
		}
	}
	for _, auth := range authSeen {
		if auth != "Bearer test-token" {
			t.Fatalf("鉴权头错误: %q", auth)
		}
	}
}

func TestSolveTurnstile映射服务端错误(t *testing.T) {
	ts, srv := newTokenService(t)
	ts.status = http.StatusBadRequest
	c, err := New(srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}

	_, err = c.SolveTurnstile(context.Background(), "https://target.example/", "0xCCCC")
	if err == nil {
		t.Fatal("期望错误")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("期望 *APIError，得到 %T: %v", err, err)
	}
	if apiErr.StatusCode != http.StatusBadRequest || apiErr.Code != "BAD_REQUEST" {
		t.Fatalf("错误字段不匹配: %+v", apiErr)
	}
	if len(apiErr.Attempts) != 1 || apiErr.Attempts[0].Backend != "camoufox" {
		t.Fatalf("尝试记录未解析: %+v", apiErr.Attempts)
	}
}

func TestSolveTurnstile拒绝空sitekey且不发请求(t *testing.T) {
	ts, srv := newTokenService(t)
	c, err := New(srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}

	if _, err := c.SolveTurnstile(context.Background(), "https://target.example/", "   "); err == nil {
		t.Fatal("空 sitekey 必须报错")
	}
	if calls, _, _ := ts.snapshot(); calls != 0 {
		t.Fatalf("空 sitekey 不应发请求，实际 %d 次", calls)
	}
}

func TestSolveTurnstile拒绝非HTTP网址(t *testing.T) {
	c, err := New("http://127.0.0.1:1", "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.SolveTurnstile(context.Background(), "ftp://target.example/", "0xAAAA"); err == nil {
		t.Fatal("非 http(s) 网址必须报错")
	}
}

func TestSolveTurnstile拒绝空令牌响应(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"want":"turnstile","token":"","user_agent":"UA","domain":"target.example"}`))
	}))
	t.Cleanup(srv.Close)
	c, err := New(srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.SolveTurnstile(context.Background(), "https://target.example/", "0xDDDD"); err == nil {
		t.Fatal("空令牌必须报错")
	}
}
