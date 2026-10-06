# Groundcrew

Groundcrew is an AI DevOps team made of NVIDIA Nemotron agents. It connects to a customer's existing
infrastructure on any cloud (AWS, Azure, Kubernetes, VMs, Vercel), maps everything into one cross-cloud
resource graph, deploys apps, and investigates and fixes incidents.

**Built with NVIDIA models on Nebius Token Factory.**

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and npm
- Docker with Docker Compose
- make
- A [Nebius Token Factory](https://docs.tokenfactory.nebius.com/) API key

## Quickstart

```bash
cp .env.example .env    # then fill in TOKEN_FACTORY_* and MODEL_* values
make install            # install API and web dependencies locally
make dev                # api on http://localhost:8000, web on http://localhost:3000
make models             # one tiny test call to each configured Nemotron model
```

Other commands: `make test`, `make lint`, `make format`, `make down`.

## Repository layout

- `apps/api`: FastAPI control plane (Python 3.12, uv)
- `apps/web`: Next.js 15 frontend (TypeScript, Tailwind, shadcn/ui)
- `scripts`: developer scripts, including the Token Factory smoke test
- `infra`: customer connection templates and our hosting setup
- `demo`: sample app and demo cloud environments

## Architecture

TODO

## Demo

TODO

## Hackathon notes

TODO

## License

License: MIT. See [LICENSE](LICENSE).
