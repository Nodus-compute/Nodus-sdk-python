"""Produce and consume declared outputs on paid GPU workloads."""
import argparse
from pathlib import Path
import nodus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--budget", type=float, default=None, help="Deprecated compatibility option")
    args = parser.parse_args()
    stages = [
        nodus.StageSpec(
            id="prepare",
            source=nodus.Source(
                image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
                command=["python", "-c", "from pathlib import Path\nPath('numbers.json').write_text('[1, 2, 3]')"],
            ),
            outputs={"numbers": "numbers.json"},
        ),
        nodus.StageSpec(
            id="summarize", depends_on=["prepare"],
            inputs=[nodus.StageInput(name="numbers", from_stage="prepare", from_output="numbers")],
            source=nodus.Source(
                image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
                command=["python", "-c", "import json, os, torch\nfrom pathlib import Path\nvalues=json.loads(Path(os.environ['NODUS_INPUT_numbers']).read_text())\nPath('result.json').write_text(json.dumps({'sum': torch.tensor(values, device='cuda').sum().item()}))"],
            ),
            outputs={"result": "result.json"},
        ),
    ]
    with nodus.Client() as client:
        workload = client.run(
            stages=stages,
            budget=args.budget, idempotency_key=f"pipeline-{args.submission_id}",
        )
        print(workload.id, flush=True)
        done = workload.wait(poll_seconds=5)
        if not done.succeeded:
            return 1
        Path("results").mkdir(exist_ok=True)
        print(workload.download_output("result", "results/result.json", stage="summarize"))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
