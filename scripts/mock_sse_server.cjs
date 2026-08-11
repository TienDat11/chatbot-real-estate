// Mock FastAPI: POST /query trả SSE stream theo format §16.2 để test FE.
const http = require("http");

const server = http.createServer((req, res) => {
  if (req.method === "POST" && req.url === "/query") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      // body chỉ cần đọc hết để trigger 'end' — payload không dùng trong mock.
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      const send = (ev, data) => {
        res.write(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`);
      };

      send("sources", [
        {
          doc_id: "doc-1",
          title: "Luật Đất đai 2024",
          section: "Điều 27",
          effective_from: "2025-01-01",
          kind: "văn bản pháp luật",
        },
        {
          doc_id: "doc-2",
          title: "BLDS 2015",
          section: "Điều 123",
          effective_from: "2017-01-01",
          kind: "văn bản pháp luật",
        },
      ]);
      send("facts", [
        {
          fe_id: "fe-1",
          subject: "Thế chấp quyền sử dụng đất",
          policy_key: "LĐĐ 2024 Điều 27",
          fields: { "Giá trị hợp đồng": 1500000000, "Lãi suất": "9.5%/năm" },
          note: "Cần thỏa thuận ba bên",
        },
      ]);
      setTimeout(() => send("token", { text: "Theo **Luật Đất đai 2024** Điều 27, quyền sử dụng đất có thể được thế chấp." }), 50);
      setTimeout(() => send("token", { text: "\n\nCần chuẩn bị: sổ đỏ, hợp đồng thế chấp, giấy tờ tùy thân." }), 150);
      setTimeout(
        () =>
          send("done", {
            trace_id: "trace-abc123",
            latency_ms: 1234,
            confidence: "MEDIUM",
            requires_review: true,
          }),
        250
      );
      setTimeout(() => res.end(), 300);
    });
    return;
  }
  res.writeHead(404);
  res.end();
});

server.listen(8000, () => console.log("mock SSE server on :8000"));
