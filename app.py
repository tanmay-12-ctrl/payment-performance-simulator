import streamlit as st
import asyncio, time, random, math
import numpy as np
import pandas as pd
from datetime import datetime
from io import BytesIO

# ---------- Optional PDF export ----------
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False

st.set_page_config(page_title="Payment Performance Simulator ", layout="wide")
st.title("💳 Payment Transaction Performance Simulator — Pro+")

# ==================== SIDEBAR CONTROLS ====================
with st.sidebar:
    st.header("Test Window")
    duration_sec = st.slider("Test Duration (sec)", 10, 180, 30)

    st.header("Load Profile")
    profile = st.selectbox("Arrival Pattern", ["Constant", "Diurnal Waves", "Bursty"])
    base_tps = st.slider("Base TPS", 1, 800, 150)

    st.markdown("**Diurnal (if selected)**")
    diurnal_peak_mult = st.slider("Peak Multiplier", 1.0, 4.0, 2.0)
    diurnal_cycles = st.slider("Waves in Test Window", 1, 6, 2)  # how many peaks across the duration

    st.markdown("**Bursts (if selected)**")
    burst_prob_per_sec = st.slider("Burst Chance per Sec (%)", 0, 100, 10) / 100.0
    burst_multiplier = st.slider("Burst Multiplier", 1.0, 8.0, 3.0)
    burst_duration_s = st.slider("Burst Duration (sec)", 1, 20, 5)

    st.header("Capacity / Concurrency")
    max_conc = st.slider("Max Concurrency (server pool size)", 1, 1000, 80)

    st.header("Service Time (Lognormal)")
    median_ms = st.slider("Median Service Time (ms)", 10, 600, 90)
    tail_sigma = st.slider("Tail Heaviness σ", 0, 200, 60) / 100.0  # 0..2.0

    st.header("Client & SLO")
    client_timeout_ms = st.slider("Client Timeout (ms)", 50, 3000, 600)
    sla_ms = st.slider("SLA Threshold (ms)", 50, 3000, 250)
    apdex_T_ms = st.slider("Apdex T (ms)", 50, 800, 200)
    slo_target = st.selectbox("SLO (success ≤ SLA)", ["99.0%", "99.5%", "99.9%"], index=0)
    slo_target = float(slo_target.strip("%"))/100.0

    st.header("Base Failures")
    base_sys_err = st.slider("System Error Rate %", 0, 20, 3) / 100.0
    biz_decline = st.slider("Business Decline %", 0, 20, 2) / 100.0

    st.header("Error Spikes Under Load")
    spike_threshold_util = st.slider("Spike Above Utilization", 50, 100, 80) / 100.0
    spike_sys_boost = st.slider("System Error Boost % (when spiking)", 0, 200, 50) / 100.0

    st.header("Transaction Mix & Skew")
    skew_types = st.checkbox("Enable Type-specific Failure Skew", True)
    type_weights = {
        "AUTH": st.slider("AUTH weight", 0, 100, 60),
        "CAPTURE": st.slider("CAPTURE weight", 0, 100, 20),
        "REFUND": st.slider("REFUND weight", 0, 100, 15),
        "REVERSAL": st.slider("REVERSAL weight", 0, 100, 5),
    }
    # Extra decline % per type (only if skew enabled)
    type_decline_extra = {
        "AUTH": st.slider("AUTH extra decline %", 0, 10, 0) / 100.0,
        "CAPTURE": st.slider("CAPTURE extra decline %", 0, 10, 1) / 100.0,
        "REFUND": st.slider("REFUND extra decline %", 0, 10, 2) / 100.0,
        "REVERSAL": st.slider("REVERSAL extra decline %", 0, 10, 4) / 100.0,
    }

    st.header("Retries (Exponential Backoff)")
    enable_retries = st.checkbox("Enable Retries", True)
    max_retries = st.slider("Max Retries", 0, 5, 2)
    backoff_base_ms = st.slider("Backoff Base (ms)", 50, 1500, 150)
    backoff_factor = st.slider("Backoff Factor", 1.5, 4.0, 2.0)

    st.header("Randomness")
    seed = st.number_input("Random Seed", value=42, step=1)

run = st.button("Run Simulation")

# ==================== RNG ====================
rng = random.Random(seed)
np_rng = np.random.default_rng(seed)

def sample_service_ms(median_ms: float, sigma: float) -> float:
    mu = math.log(max(1e-6, median_ms))
    return float(np_rng.lognormal(mean=mu, sigma=sigma))

def choose_type(weights: dict) -> str:
    items = list(weights.items())
    labels, ws = zip(*items)
    s = sum(ws) or 1
    pick = rng.uniform(0, s)
    acc = 0
    for lab, w in items:
        acc += w
        if pick <= acc:
            return lab
    return labels[-1]

# Track active concurrency (for utilization) without depending on private attrs
class ActiveCounter:
    def __init__(self): self.active = 0
    def inc(self): self.active += 1
    def dec(self): self.active -= 1
active_counter = ActiveCounter()

async def process_once(sema: asyncio.Semaphore, service_ms: float, sys_err: float, biz_dec: float):
    t0 = time.perf_counter()
    waited_ms = None
    # queue wait
    async with sema:
        active_counter.inc()
        try:
            t1 = time.perf_counter()
            waited_ms = (t1 - t0) * 1000.0
            await asyncio.sleep(service_ms / 1000.0)
            r = rng.random()
            if r < biz_dec:
                return "BUSINESS_DECLINE", waited_ms, service_ms
            elif r < biz_dec + sys_err:
                return "SYSTEM_ERROR", waited_ms, service_ms
            else:
                return "SUCCESS", waited_ms, service_ms
        finally:
            active_counter.dec()

async def do_attempt_with_timeout(sema, median_ms, sigma, sys_err, biz_dec, timeout_ms):
    svc = sample_service_ms(median_ms, sigma)
    try:
        return await asyncio.wait_for(
            process_once(sema, svc, sys_err, biz_dec),
            timeout=timeout_ms / 1000.0
        )
    except asyncio.TimeoutError:
        return "TIMEOUT", None, None

async def handle_request(req_id: int, arrival_ts: float, sema, cfg, txn_type: str):
    attempts, waits, svcs = 0, [], []
    last_status = None

    # Type-specific decline skew
    type_extra = cfg["type_decline_extra"].get(txn_type, 0.0) if cfg["skew_types"] else 0.0

    while True:
        attempts += 1

        # Dynamic system error boost under high utilization
        util = min(1.0, active_counter.active / max(1, cfg["max_conc"]))
        sys_err_eff = cfg["base_sys_err"] * (1.0 + (cfg["spike_sys_boost"] if util >= cfg["spike_threshold_util"] else 0.0))
        biz_dec_eff = min(1.0, cfg["biz_decline"] + type_extra)

        status, wait_ms, svc_ms = await do_attempt_with_timeout(
            sema, cfg["median_ms"], cfg["sigma"], sys_err_eff, biz_dec_eff, cfg["client_timeout_ms"]
        )
        if wait_ms is not None: waits.append(wait_ms)
        if svc_ms  is not None: svcs.append(svc_ms)

        if status in ("SUCCESS", "BUSINESS_DECLINE"):
            last_status = status
            break

        if not cfg["enable_retries"] or attempts > cfg["max_retries"]:
            last_status = status
            break

        backoff = cfg["backoff_base_ms"] * (cfg["backoff_factor"] ** (attempts - 1))
        await asyncio.sleep(backoff / 1000.0)

    end = time.perf_counter()
    return {
        "req_id": req_id,
        "txn_type": txn_type,
        "start_ts": arrival_ts,
        "end_ts": end,
        "attempts": attempts,
        "final_status": last_status,
        "latency_ms": (end - arrival_ts) * 1000.0,
        "wait_ms": sum(waits) if waits else 0.0,
        "service_ms": sum(svcs) if svcs else 0.0,
        "had_queue_wait": (sum(waits) if waits else 0.0) > 0.5,
    }

# ---------- Dynamic arrival rate (per-second) ----------
def rate_for_second(sec_idx: int, base_tps: float) -> float:
    if profile == "Constant":
        return base_tps
    if profile == "Diurnal Waves":
        # Sine wave across the test duration with chosen cycles
        # value in [1/peak_mult, peak_mult]
        x = (sec_idx / max(1, duration_sec)) * diurnal_cycles * 2 * math.pi
        amp = (diurnal_peak_mult - 1.0)
        return base_tps * (1.0 + amp * (0.5 * (1 + math.sin(x)) - 0.5) * 2)  # center around base
    # Bursty
    return base_tps  # bursts handled separately

async def arrival_loop(duration_sec: int, base_tps: float, handler_coro, weights: dict):
    tasks = []
    req_id = 0
    burst_end_at = -1
    progress = st.progress(0, text="Generating load...")
    start_wall = time.perf_counter()

    for sec in range(duration_sec):
        # Determine TPS this second
        lam = rate_for_second(sec, base_tps)
        # Apply burst regime
        if profile == "Bursty":
            # Are we in a burst?
            now = sec
            if now <= burst_end_at:
                lam *= burst_multiplier
            else:
                # chance to start a burst
                if rng.random() < burst_prob_per_sec:
                    burst_end_at = now + int(burst_duration_s)
                    lam *= burst_multiplier

        # Draw arrivals ~ Poisson(lam)
        arrivals = np_rng.poisson(lam= max(0.0, lam))
        # Spread them uniformly within this second
        deltas = sorted([rng.random() for _ in range(arrivals)])
        for d in deltas:
            req_id += 1
            txn_type = choose_type(weights)
            async def delayed_start(delay_s, _id=req_id, _type=txn_type):
                await asyncio.sleep(delay_s)
                return await handler_coro(_id, time.perf_counter(), _type)
            tasks.append(asyncio.create_task(delayed_start(d)))

        # small tick so UI can update the progress bar
        progress.progress(int((sec + 1) / duration_sec * 100), text=f"Generating & running... ({sec+1}s/{duration_sec}s)")
        await asyncio.sleep(1.0)  # next second

    # Wait for all outstanding tasks to complete
    results = await asyncio.gather(*tasks, return_exceptions=False)
    progress.empty()
    end_wall = time.perf_counter()
    return results, (end_wall - start_wall)

def summarize(df: pd.DataFrame, duration_sec: int, sla_ms: float, apdex_T_ms: float, slo_target: float):
    if df.empty:
        return {}, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, None

    # Per-second TPS (success only)
    t0 = df["start_ts"].min()
    df["sec"] = (df["end_ts"] - t0).astype(float).apply(lambda x: int(max(0, x)))
    tps_sec = df[df["final_status"] == "SUCCESS"].groupby("sec")["req_id"].count()
    tps_sec = tps_sec.reindex(range(int(df["sec"].max()) + 1), fill_value=0)

    # Percentiles (success)
    lat_succ = df[df["final_status"] == "SUCCESS"]["latency_ms"]
    p50 = np.percentile(lat_succ, 50) if len(lat_succ) else np.nan
    p95 = np.percentile(lat_succ, 95) if len(lat_succ) else np.nan
    p99 = np.percentile(lat_succ, 99) if len(lat_succ) else np.nan
    lat_max = lat_succ.max() if len(lat_succ) else np.nan

    # SLA compliance %
    sla_ok = ((df["final_status"] == "SUCCESS") & (df["latency_ms"] <= sla_ms)).mean() * 100.0

    # Apdex
    S = ((df["final_status"] == "SUCCESS") & (df["latency_ms"] <= apdex_T_ms)).sum()
    T = ((df["final_status"] == "SUCCESS") & (df["latency_ms"].between(apdex_T_ms, 4*apdex_T_ms))).sum()
    N = len(df)
    apdex = (S + 0.5*T) / N if N else 0.0

    # Error taxonomy
    counts = df["final_status"].value_counts()
    succ = int(counts.get("SUCCESS", 0))
    tmo  = int(counts.get("TIMEOUT", 0))
    sys  = int(counts.get("SYSTEM_ERROR", 0))
    biz  = int(counts.get("BUSINESS_DECLINE", 0))

    # Throughput drop % vs best second
    best = tps_sec.max() if len(tps_sec) else 0
    avg = tps_sec.mean() if len(tps_sec) else 0
    drop_pct = ((best - avg) / best * 100.0) if best > 0 else 0.0

    # Retry amplification
    amplification = df["attempts"].sum() / len(df) if len(df) else 0.0

    # Queueing stats
    waited_frac = df["had_queue_wait"].mean() * 100.0
    avg_wait_ms = df["wait_ms"].mean()
    avg_svc_ms  = df["service_ms"].mean()
    max_wait_ms = df["wait_ms"].max()

    # Overall TPS
    overall_tps = succ / duration_sec if duration_sec > 0 else 0.0

    # Error budget burn
    bad = ((df["final_status"] != "SUCCESS") | (df["latency_ms"] > sla_ms)).mean()
    err_budget = 1.0 - slo_target
    burn_rate  = (bad / err_budget) if err_budget > 0 else np.nan

    # Recovery time after incident:
    # define an "incident second" when either utilization likely high (approx via wait_ms>0) or error rate spikes.
    per_sec = df.groupby("sec").agg(
        success=("final_status", lambda s: (s=="SUCCESS").sum()),
        total=("final_status", "count"),
        sla_pass=("latency_ms", lambda x: (x <= sla_ms).sum()),
        waits=("wait_ms", "mean")
    ).reset_index()
    per_sec["sla_pct"] = np.where(per_sec["total"]>0, per_sec["sla_pass"]/per_sec["total"], 0.0)

    # Consider incident seconds as those with sla_pct < slo_target or waits > tiny threshold
    incident_secs = per_sec[ (per_sec["sla_pct"] < slo_target) | (per_sec["waits"] > 0.5) ]["sec"].tolist()
    recovery_time_s = None
    incident_end_s = None
    if incident_secs:
        incident_end_s = max(incident_secs)
        # recovery when from some second k onwards next 3s meet SLO
        for k in range(incident_end_s+1, int(per_sec["sec"].max())-2):
            window = per_sec[(per_sec["sec"]>=k) & (per_sec["sec"]<k+3)]
            if len(window)>=3 and (window["sla_pct"] >= slo_target).all():
                recovery_time_s = k - incident_end_s
                break

    metrics = {
        "Total Requests": int(len(df)),
        "Success": succ, "Timeout": tmo, "System Error": sys, "Business Decline": biz,
        "Overall TPS": round(overall_tps, 2),
        "p50 Latency (ms)": round(p50, 2) if not math.isnan(p50) else None,
        "p95 Latency (ms)": round(p95, 2) if not math.isnan(p95) else None,
        "p99 Latency (ms)": round(p99, 2) if not math.isnan(p99) else None,
        "Max Latency (ms)": round(lat_max, 2) if not (isinstance(lat_max, float) and math.isnan(lat_max)) else None,
        "SLA ≤ {}ms (%)".format(sla_ms): round(sla_ok, 2),
        "Apdex (T={}ms)".format(apdex_T_ms): round(apdex, 3),
        "Retry Amplification (x)": round(amplification, 3),
        "Waited >0ms (%)": round(waited_frac, 2),
        "Avg Wait (ms)": round(avg_wait_ms, 2),
        "Avg Service (ms)": round(avg_svc_ms, 2),
        "Max Wait (ms)": round(max_wait_ms, 2),
        "Throughput Drop vs Peak (%)": round(drop_pct, 2),
        "Error Budget Burn (1.0=on budget)": round(burn_rate, 2),
        "Recovery Time After Incident (s)": int(recovery_time_s) if recovery_time_s is not None else None,
    }

    pct_table = pd.DataFrame({"p50":[p50], "p95":[p95], "p99":[p99], "max":[lat_max]}).round(2)
    tps_table = pd.DataFrame({"sec": tps_sec.index, "success_tps": tps_sec.values})
    by_type = df.groupby(["txn_type","final_status"]).size().unstack(fill_value=0)
    return metrics, pct_table, tps_table, by_type, incident_end_s, per_sec

def make_pdf(metrics: dict, tps_table: pd.DataFrame, file_name="report.pdf") -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Payment Performance Report")
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(40, y, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    y -= 24
    for k, v in metrics.items():
        txt = f"{k}: {v}"
        c.drawString(40, y, txt[:100])
        y -= 14
        if y < 60:
            c.showPage(); y = h - 40; c.setFont("Helvetica", 10)
    c.showPage(); c.save()
    buf.seek(0)
    return buf.read()

# ==================== RUN ====================
if run:
    cfg = {
        "median_ms": median_ms, "sigma": tail_sigma,
        "base_sys_err": base_sys_err, "biz_decline": biz_decline,
        "client_timeout_ms": client_timeout_ms,
        "enable_retries": enable_retries, "max_retries": max_retries,
        "backoff_base_ms": backoff_base_ms, "backoff_factor": backoff_factor,
        "skew_types": skew_types, "type_decline_extra": type_decline_extra,
        "max_conc": max_conc, "spike_threshold_util": spike_threshold_util, "spike_sys_boost": spike_sys_boost
    }

    sema = asyncio.Semaphore(max_conc)

    async def handler(req_id, arrival_ts, txn_type):
        return await handle_request(req_id, arrival_ts, sema, cfg, txn_type)

    async def run_test():
        results, wall = await arrival_loop(duration_sec, base_tps, handler, type_weights)
        return results, wall

    with st.spinner("Running simulation..."):
        records, wall = asyncio.run(run_test())

    df = pd.DataFrame(records)
    metrics, pct_table, tps_table, by_type, incident_end_s, per_sec = summarize(df, duration_sec, sla_ms, apdex_T_ms, slo_target)

    # ==================== DASHBOARD ====================
    top = st.columns(4)
    top[0].metric("Overall TPS", metrics.get("Overall TPS"))
    top[1].metric(f"SLA ≤ {sla_ms}ms (%)", metrics.get(f"SLA ≤ {sla_ms}ms (%)"))
    top[2].metric("Apdex", metrics.get(f"Apdex (T={apdex_T_ms}ms)"))
    top[3].metric("Retry Amplification (x)", metrics.get("Retry Amplification (x)"))

    mid = st.columns(4)
    mid[0].metric("p50 (ms)", metrics.get("p50 Latency (ms)"))
    mid[1].metric("p95 (ms)", metrics.get("p95 Latency (ms)"))
    mid[2].metric("p99 (ms)", metrics.get("p99 Latency (ms)"))
    mid[3].metric("Max Latency (ms)", metrics.get("Max Latency (ms)"))

    bot = st.columns(4)
    bot[0].metric("Throughput Drop vs Peak (%)", metrics.get("Throughput Drop vs Peak (%)"))
    bot[1].metric("Waited >0ms (%)", metrics.get("Waited >0ms (%)"))
    bot[2].metric("Avg Wait (ms)", metrics.get("Avg Wait (ms)"))
    bot[3].metric("Error Budget Burn", metrics.get("Error Budget Burn (1.0=on budget)"))

    st.metric("Recovery Time After Incident (s)", metrics.get("Recovery Time After Incident (s)"))

    st.subheader("Latency Percentiles (ms)")
    st.table(pct_table)

    st.subheader("TPS per Second (success only)")
    st.line_chart(tps_table.set_index("sec"))

    st.subheader("Outcome Breakdown by Transaction Type")
    st.bar_chart(by_type)

    st.subheader("Latency Histogram (Success Only)")
    st.bar_chart(pd.DataFrame({"latency_ms": df[df["final_status"]=="SUCCESS"]["latency_ms"]}))

    with st.expander("Per-second SLO View / Raw Data"):
        if per_sec is not None:
            st.line_chart(per_sec.set_index("sec")[["sla_pct"]])
        st.dataframe(df.head(1000))

        # Downloads
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv, file_name="simulation_results.csv", mime="text/csv")

        if HAVE_PDF:
            pdf_bytes = make_pdf(metrics, tps_table, "report.pdf")
            st.download_button("Download PDF Report", data=pdf_bytes, file_name="performance_report.pdf", mime="application/pdf")
        else:
            st.caption("PDF export requires 'reportlab' (add to requirements.txt).")

    st.caption("Features: non-homogeneous arrivals (diurnal/bursty), bounded concurrency, heavy-tailed service, client timeouts, retries with exponential backoff, error spikes under high utilization, type-skewed declines, p95/p99, throughput drop, SLO compliance, recovery time, CSV/PDF export.")
else:
    st.info("Set parameters on the left, then click **Run Simulation**.")
