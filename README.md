# Titan-U Core Microservices (Manoat)

![System Status](https://img.shields.io/badge/Status-Production_Ready-brightgreen)
![Architecture](https://img.shields.io/badge/Architecture-Distributed_Microservices-blue)
![License](https://img.shields.io/badge/License-Proprietary-red)

> **Sovereign, local-first AI execution environment with dynamic routing, machine-learning policy enforcement, and isolated Docker sandboxing.**

Titan-U (Manoat) is the foundational microservice architecture for decentralized, autonomous AI operations. It is designed to execute locally, keeping all data sovereign and bypassing external API dependencies. The ecosystem orchestrates LLM inference, trains k-NN models on agent trajectories, and executes code in secure, ephemeral environments.

---

## 📑 Table of Contents

- [System Architecture](#-system-architecture)
- [Microservices Overview](#-microservices-overview)
- [Prerequisites](#-prerequisites)
- [Rapid Deployment](#-rapid-deployment)
- [Core API Endpoints](#-core-api-endpoints)
- [License & Intellectual Property](#-license--intellectual-property)

---

## 🏗 System Architecture

The ecosystem relies on three decoupled FastAPI microservices. To prevent port collision and enable horizontal scalability, each module is hard-locked to a dedicated port. 

*   **Language Engine:** Python 3.11+
*   **API Framework:** FastAPI / Uvicorn
*   **Database:** SQLite (SQLAlchemy ORM)
*   **Virtualization:** Docker Engine API (v1.44)
*   **Machine Learning:** Scikit-learn (TF-IDF & k-NN)

---

## ⚙️ Microservices Overview

### 1. Model Router (Port `8001`)
Dynamically routes inference requests to either local `ollama` or `llama.cpp` backends based on task type (chat, code, summary), cost constraints, and latency requirements.

### 2. Policy Engine (Port `8002`)
The brain of the agentic system. It stores agent trajectories and trains real-time k-Nearest Neighbors (k-NN) classifiers to predict optimal actions based on vectorized text inputs. 

### 3. TitanSand VM (Port `8003`)
A secure virtualization layer that spins up isolated Docker containers for code execution and autonomous research. Supports air-gapped, proxy-only, and full network modes to prevent malicious code breakouts.

---

## 📦 Prerequisites

Ensure the host machine is configured with the following before deployment:

*   **Python 3.11+**
*   **Docker Desktop** (Must be running. The system uses `npipe:////./pipe/docker_engine` for Windows host communication).
*   **Ollama / Llama.cpp** (Ensure local models like `llama3` and `codellama` are pulled and running if executing inference).

---

## 🚀 Rapid Deployment

The system is designed for instant, zero-configuration booting via PowerShell.

1. Clone or unpack the repository into your designated command unit drive (e.g., `E:\titanU-manoat`).
2. Open PowerShell as Administrator.
3. Execute the automated boot script:

```powershell
cd E:\titanU-manoat
.\run_ecosystem.ps1
