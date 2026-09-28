"""Submit paid work with a stable ID retained across CI retries."""
import argparse
import nodus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--budget", type=float, default=None, help="Deprecated compatibility option")
    args = parser.parse_args()
    with nodus.Client() as client:
        workload = client.run(
            image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
            command=["python", "-c", "import torch\nassert torch.arange(10, device='cuda').sum().item() == 45\nprint('passed')"],
            budget=args.budget,
            idempotency_key=f"ci-{args.submission_id}",
        )
        print(workload.id, flush=True)
        done = workload.wait(poll_seconds=5)
        return 0 if done.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
