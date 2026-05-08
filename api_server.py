#!/usr/bin/env python3
"""
Simple HTTP server to serve latest arbitrage scanner results.
Runs on port 8080 on the VPS.
"""

import http.server
import json
import os
import sys
from datetime import datetime

BASE_DIR = "/root/arb-scanner"
PORT = 8080

class ArbAPIHandler(http.server.BaseHTTPRequestHandler):
    
    def do_GET(self):
        cors = ("Access-Control-Allow-Origin", "*")
        
        if self.path == "/api/arb/latest":
            # Read latest scan results
            data = {"status": "ok"}
            scan_file = os.path.join(BASE_DIR, "last_scan.json")
            trades_file = os.path.join(BASE_DIR, "paper_trades.json")
            enriched_file = os.path.join(BASE_DIR, "data", "enriched.json")
            
            if os.path.exists(scan_file):
                with open(scan_file) as f:
                    data["scan"] = json.load(f)
            
            if os.path.exists(trades_file):
                with open(trades_file) as f:
                    data["paper_trades"] = json.load(f)
            
            if os.path.exists(enriched_file):
                with open(enriched_file) as f:
                    data["enriched"] = json.load(f)
            
            body = json.dumps(data).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/enriched":
            enriched_file = os.path.join(BASE_DIR, "data", "enriched.json")
            if os.path.exists(enriched_file):
                with open(enriched_file) as f:
                    body = json.dumps(json.load(f)).encode()
            else:
                body = json.dumps({"status": "no_data"}).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/crypto":
            scan_file = os.path.join(BASE_DIR, "last_scan.json")
            if os.path.exists(scan_file):
                with open(scan_file) as f:
                    data = json.load(f)
                body = json.dumps(data.get("crypto", {})).encode()
            else:
                body = json.dumps({"status": "no_data"}).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/polymarket":
            scan_file = os.path.join(BASE_DIR, "last_scan.json")
            if os.path.exists(scan_file):
                with open(scan_file) as f:
                    data = json.load(f)
                body = json.dumps(data.get("polymarket", {})).encode()
            else:
                body = json.dumps({"status": "no_data"}).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/trades":
            trades_file = os.path.join(BASE_DIR, "paper_trades.json")
            if os.path.exists(trades_file):
                with open(trades_file) as f:
                    body = json.dumps(json.load(f)).encode()
            else:
                body = json.dumps([]).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/funding-trades":
            trades_file = os.path.join(BASE_DIR, "paper_trades.json")
            if os.path.exists(trades_file):
                with open(trades_file) as f:
                    body = json.dumps(json.load(f)).encode()
            else:
                body = json.dumps([]).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/funding-v3":
            funding_file = os.path.join(BASE_DIR, "funding_v3_trades.json")
            if os.path.exists(funding_file):
                with open(funding_file) as f:
                    body = json.dumps(json.load(f)).encode()
            else:
                body = json.dumps([]).encode()
            self._send(200, body, cors)

        elif self.path == "/api/arb/history":
            history_file = os.path.join(BASE_DIR, "data", "history.json")
            if os.path.exists(history_file):
                with open(history_file) as f:
                    body = json.dumps(json.load(f)).encode()
            else:
                body = json.dumps({"crypto": [], "polymarket": [], "timestamps": []}).encode()
            self._send(200, body, cors)
            
        elif self.path == "/api/arb/health":
            self._send(200, json.dumps(self._health_check()).encode(), cors)

        elif self.path == "/health":
            self._send(200, json.dumps({"status": "ok", "service": "arb-api"}).encode(), cors)
            
        else:
            self._send(404, json.dumps({"error": "not found"}).encode())
    
    def do_POST(self):
        cors = ("Access-Control-Allow-Origin", "*")
        content_length = int(self.headers.get("Content-Length", 0))
        
        if self.path == "/api/arb/enriched" and content_length > 0:
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                enriched_dir = os.path.join(BASE_DIR, "data")
                os.makedirs(enriched_dir, exist_ok=True)
                enriched_file = os.path.join(enriched_dir, "enriched.json")
                with open(enriched_file, "w") as f:
                    json.dump(data, f, indent=2)
                self._send(200, json.dumps({"status": "accepted"}).encode(), cors)
            except json.JSONDecodeError:
                self._send(400, json.dumps({"error": "invalid json"}).encode())
        else:
            self._send(404, json.dumps({"error": "not found"}).encode(), cors)
    
    def _send(self, code, body, cors=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cors:
            self.send_header(*cors)
        self.end_headers()
        self.wfile.write(body)
    
    def _health_check(self):
        """Return comprehensive health summary of all trading bots."""
        import time
        now = time.time()
        result = {"status": "ok", "service": "arb-api", "timestamp": datetime.utcnow().isoformat()}
        
        # 1. API server start time
        try:
            with open("/proc/self/stat") as f:
                parts = f.read().split()
                start_ticks = int(parts[21])
            # SC_CLK_TCK is typically 100 on Linux
            result["api_start_time_ticks"] = start_ticks
        except:
            pass
        
        # 2. Check scan freshness — try history.json first, fall back to last_scan.json
        history_file = os.path.join(BASE_DIR, "data", "history.json")
        scan_file = os.path.join(BASE_DIR, "last_scan.json")
        
        # Check history.json for latest scan time
        if os.path.exists(history_file):
            try:
                with open(history_file) as f:
                    history = json.load(f)
                timestamps = history.get("timestamps", [])
                if timestamps:
                    latest_ts = timestamps[-1]
                    # history.json timestamps have no timezone — assume UTC
                    scan_time = datetime.fromisoformat(latest_ts)
                    age_hours = (datetime.utcnow() - scan_time).total_seconds() / 3600
                    crypto_count = history.get("crypto", [])
                    poly_count = history.get("polymarket", [])
                    result["scan"] = {"last_run": latest_ts, "age_hours": round(age_hours, 1),
                                      "crypto_opportunities": crypto_count[-1] if crypto_count else 0,
                                      "polymarket_opportunities": poly_count[-1] if poly_count else 0}
                    if age_hours > 1:
                        result["scan"]["warning"] = "Scan older than 1 hour"
            except Exception as e:
                result["scan"] = {"status": "error", "message": str(e)}
        
        # Also check last_scan.json for older format data
        if os.path.exists(scan_file):
            try:
                with open(scan_file) as f:
                    scan = json.load(f)
                if "scan" not in result:
                    result["scan"] = {}
                result["scan"]["last_scan_json_ts"] = scan.get("timestamp", "")
                result["scan"]["total_opportunities"] = scan.get("total_opportunities", 0)
            except:
                pass
        
        # 3. Check trade files freshness
        for name, fpath in [("spot_perp_arb", "paper_trades.json"), ("funding_v3", "funding_v3_trades.json")]:
            fp = os.path.join(BASE_DIR, fpath)
            if os.path.exists(fp):
                mtime = os.path.getmtime(fp)
                age_mins = (now - mtime) / 60
                result[name] = {"last_modified_mins_ago": round(age_mins, 0)}
                try:
                    with open(fp) as f:
                        trades = json.load(f)
                    entries = [t for t in trades if t.get("type") == "ENTRY"]
                    exits = [t for t in trades if t.get("type") == "EXIT"]
                    result[name]["total_trades"] = len(trades)
                    result[name]["entries"] = len(entries)
                    result[name]["exits"] = len(exits)
                    if exits:
                        pnl = sum(t.get("pnl_usdt", 0) or 0 for t in exits)
                        result[name]["realized_pnl"] = round(pnl, 2)
                    open_symbols = set(t.get("symbol","") for t in entries) - set(t.get("symbol","") for t in exits)
                    result[name]["open_positions"] = len([s for s in open_symbols if s])
                except:
                    result[name]["error"] = "corrupt data"
        
        return result
    
    def log_message(self, format, *args):
        print(f"[ArbAPI] {args[0]} {args[1]}")


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), ArbAPIHandler)
    print(f"[ArbAPI] Serving on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
