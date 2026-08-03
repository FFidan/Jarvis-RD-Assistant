<!-- verified-against-UI: 2026-08-03 | routes: /setup, /onboarding -->

# Quick start

Get JARVIS RD Assistant running on your own machine and analyze your first paper. This is the happy path for Linux or macOS with Docker. For anything non-standard — GPUs, remote or family access, non-interactive installs — see the [Deployment guide](../DEPLOYMENT.md).

## What you get

A self-hosted research workspace: a daily ranked feed of new papers, cross-paper question answering with inline citations, and spaced-repetition cards — running on your own hardware with local Ollama models.

## Before you run

- **Docker** Engine 24+ with Compose 2.24+ (Docker Desktop on macOS, or WSL2 on Windows)
- **~27–54 GB** free disk for the one-time first install (models and images)
- **GPU optional** — NVIDIA is fastest; CPU works, but a first paper analysis can take 30+ minutes

## Install

```bash
git clone https://github.com/limitcycle-oss/jarvis-rd-assistant.git
cd jarvis-rd-assistant
./setup.sh
```

`setup.sh` checks your system, generates its own secrets, pulls the images and the Ollama models for your hardware, waits for the services, and prints **one finish-setup link**. Open that link in your browser.

## First few minutes in the app

1. **Create your administrator** — enter an email and choose *Create admin & sign in*. The first admin needs no email round-trip.
2. **Add a research topic** in the setup wizard so the feed has something to track.
3. **Add your first paper** — paste an arXiv link or DOI, or upload a PDF.
4. **Choose *Analyze*** on that paper to download, parse, and summarize it. On a GPU this takes a few minutes; on CPU, longer.

You now have a running instance with a paper analyzed. Explore the daily feed, ask questions across your library, and turn findings into cards.

## Next steps

- **Using the app day to day:** [First sign-in and setup](getting-started.md) covers accounts, the wizard, inviting others, and adding papers in full.
- **Something non-standard?** The [Deployment guide](../DEPLOYMENT.md) covers GPUs (NVIDIA, ROCm, Vulkan), remote and family access, macOS specifics, and unattended installs.
