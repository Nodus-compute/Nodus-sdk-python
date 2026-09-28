"""Submit a paid self-contained GPU workload after configuring Nodus credentials."""
import argparse
import nodus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, default=None, help="Deprecated compatibility option")
    args = parser.parse_args()
    with nodus.Client() as client:
        workload = client.run(
            image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
            command=["python", "-c", "import torch\nassert torch.cuda.is_available()\nprint(torch.cuda.get_device_name(0))"],
            budget=args.budget,
        )
        print(workload.id, flush=True)
        done = workload.wait()
        print(done.status, done.cost_now_usd)
        return 0 if done.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
