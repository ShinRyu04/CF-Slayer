// demo 通过 Go 客户端跑一遍 solve → replay，并打印每次回放延迟与池状态。
//
//	CF_SLAYER_SERVICES=http://127.0.0.1:8878,http://127.0.0.1:8879 \
//	CF_SLAYER_AUTH_TOKEN=... go run ./examples/demo -url https://target.example/ -n 5
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"time"

	cfclient "cfslayer/client"
)

func main() {
	target := flag.String("url", "https://target.example/", "目标网址")
	count := flag.Int("n", 5, "回放次数")
	timeout := flag.Duration("timeout", 30*time.Second, "单次请求超时")
	interval := flag.Duration("interval", 0, "每次回放之间的间隔（观察池状态用）")
	flag.Parse()

	if *count < 1 {
		log.Fatal("-n 必须 >= 1")
	}
	pool, err := cfclient.NewPoolFromEnv()
	if err != nil {
		log.Fatalf("创建池失败: %v", err)
	}
	defer pool.Close()

	start := time.Now()
	solveCtx, cancelSolve := context.WithTimeout(context.Background(), *timeout)
	cred, err := pool.Solve(solveCtx, *target)
	cancelSolve()
	if err != nil {
		log.Fatalf("求解失败: %v", err)
	}
	fmt.Printf("求解: 域名=%s cached=%v 用时=%s\n", cred.Domain, cred.Cached, time.Since(start).Round(time.Millisecond))

	prev := map[string]string{}
	for _, st := range pool.Status() {
		prev[st.URL] = st.State
	}
	for i := 1; i <= *count; i++ {
		reqCtx, cancelReq := context.WithTimeout(context.Background(), *timeout)
		req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, *target, nil)
		if err != nil {
			cancelReq()
			log.Fatalf("构造请求失败: %v", err)
		}
		begin := time.Now()
		resp, err := pool.Do(req)
		elapsed := time.Since(begin)
		if err != nil {
			cancelReq()
			log.Fatalf("第 %d 次回放失败: %v", i, err)
		}
		n, readErr := io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		cancelReq()
		if readErr != nil {
			log.Fatalf("第 %d 次读取响应失败: %v", i, readErr)
		}
		fmt.Printf("回放 %d/%d: 状态=%d 字节=%d 延迟=%s\n", i, *count, resp.StatusCode, n, elapsed.Round(time.Microsecond))
		for _, st := range pool.Status() {
			if prev[st.URL] != st.State {
				fmt.Printf("  池状态变化: %s %s -> %s\n", st.URL, prev[st.URL], st.State)
				prev[st.URL] = st.State
			}
		}
		if *interval > 0 && i < *count {
			time.Sleep(*interval)
		}
	}

	fmt.Println("池状态:")
	for _, st := range pool.Status() {
		fmt.Printf("  %s 状态=%s 连续失败=%d\n", st.URL, st.State, st.ConsecutiveFails)
	}
	if len(pool.Status()) == 0 {
		fmt.Fprintln(os.Stderr, "  （空）")
	}
}
