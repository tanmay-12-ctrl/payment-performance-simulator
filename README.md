# 💳 Payment Transaction Performance Simulator

A high-concurrency payment transaction simulator that models real-world card processing load, dynamically varies transaction throughput, injects targeted failures, and measures key industry-grade performance metrics — all visualized in real time.

This project demonstrates **performance analysis capabilities** relevant to large-scale payment networks like Mastercard, including:
- Load pattern simulation (peak vs off-peak)
- Latency analysis (p50, p95, p99)
- SLA compliance calculation
- Resilience testing via error injection

---

## 📌 Features

### **1. Dynamic Load Generation**
- Configurable **Transactions Per Second (TPS)** over time
- **Burst mode** for sudden traffic spikes
- Simulates **peak-hour demand curves**

### **2. Error Injection Scenarios**
- Failures triggered **only under high load**
- Specific transaction types have higher failure probability
- Adjustable error rates for custom test cases

### **3. Real-Time Metrics Dashboard**
- **Average latency**
- **p95 & p99 latency** (tail latency tracking)
- **Throughput** & throughput drop %
- **Error rate trends**
- SLA compliance %

### **4. Industry-Relevant Stress Testing**
- Measures **recovery time after incidents**
- Shows system stability under **gradual ramp-up load**
- Highlights performance bottlenecks

### **5. Exportable Reports**
- Download **CSV/PDF** performance summaries
- Share results for audit & benchmarking

---

## 📊 Example Use Cases

| Scenario | Purpose |
|----------|---------|
| **Peak Hour Simulation** | Understand performance during high transaction volumes |
| **Error Spike Analysis** | Test resilience when specific gateways fail |
| **Tail Latency Tracking** | Identify rare but high-impact slow transactions |
| **SLA Compliance Testing** | Ensure service level agreements are met under stress |

---

## 🚀 How to Run Locally

### **1. Clone the repository**
```bash
git clone https://github.com/tanmay-12-ctrl/payment-performance-simulator.git
cd payment-performance-simulator
Live Demo:** [Payment Performance Simulator](https://payment-performance-simulator-6vpijxypvtjmjktu8qrfec.streamlit.app/) – Try the app in your browser, no installation required.
