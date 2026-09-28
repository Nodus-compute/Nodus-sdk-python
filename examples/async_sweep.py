"""Submit three paid experiments, limiting concurrent waits to two."""
import argparse
import asyncio
import nodus


async def run(args):
    semaphore = asyncio.Semaphore(2)
    async with nodus.AsyncClient() as client:
        async def experiment(index):
            async with semaphore:
                workload = await client.run(
                    image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
                    command=["python", "-c", f"import torch\nx=torch.arange({index + 10}, device='cuda')\nprint((x*x).sum().item())"],
                    budget=args.budget_per_run,
                    idempotency_key=f"sweep-{args.run_id}-{index}",
                )
                print(index, workload.id, flush=True)
                try:
                    done = await workload.wait(poll_seconds=5)
                except asyncio.CancelledError:
                    await workload.cancel()
                    raise
                return done.succeeded
        results = await asyncio.gather(*(experiment(i) for i in range(3)))
        return 0 if all(results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--budget-per-run", type=float, default=None, help="Deprecated compatibility option")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
