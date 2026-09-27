# Run RL: an example, your own code, or a prepared recipe

## Start from an example

Every environment in the catalog lists the examples you can run today, with the
baseline and any training gain that was measured by running them:

```python
def show_examples(client):
    for environment in client.rl.list_environments():
        for example in environment.examples:
            print(environment.id, example.name, example.mode,
                  example.baseline_pass_rate, example.measured_change_pp,
                  example.runtime_minutes)
```

An environment with an empty `examples` list is listed for reference and has no
runnable command yet.

Run an example with the same command, image, output file and RL setup the
console uses. `run_arguments()` supplies everything `run()` needs:

```python
def run_example(client, *, environment_id, example_name, stable_run_id):
    environment = next(e for e in client.rl.list_environments() if e.id == environment_id)
    example = next(x for x in environment.examples if x.name == example_name)
    return client.run(idempotency_key=stable_run_id, **example.run_arguments())
```

The command is passed through a shell, as the console does, because an example
chains its install, download and run steps with `&&`.

## Run your own RL code

You do not need a catalog environment. Add an `RLSetup` to a normal run so the
console shows your reported progress and compares scores before and after:

```python
import nodus

def launch_custom_rl(
    client, *, image, source_asset_id, stable_run_id,
    model_label, planned_tasks,
):
    if not isinstance(stable_run_id, str) or not stable_run_id:
        raise ValueError("stable_run_id must be a nonempty stable key for this run")
    return client.run(
        image=image,
        source_asset_id=source_asset_id,
        command=["python", "train.py"],
        outputs={"results": "outputs"},
        compute_class="accelerator",
        idempotency_key=stable_run_id,
        rl=nodus.RLSetup(
            environment_id="custom",
            mode="train",
            model=model_label,
            planned_tasks=planned_tasks,
        ),
    )
```

Use an imported source asset containing your `train.py`, a compatible runtime
image and explicit authorization. The command must write final results
under `outputs` and write and load its own checkpoint state. Adjust the command
and output path to match your project.

`RLSetup` checks the same rules the server does, so a wrong mode or an
out-of-range task count raises `ValueError` before anything is submitted. Use
`mode="evaluate"` for evaluation without training. `planned_tasks` is how many
held-out tasks you intend to score.

Inside `train.py`, report each scored attempt with `RLEventEmitter`. It writes
one line per attempt to standard output, which Nodus turns into the live task
feed and the before-and-after comparison:

```python
import os
import time

from nodus import RLEventEmitter

def score_held_out(tasks, answer, check, phase="evaluation"):
    events = RLEventEmitter(os.environ.get("NODUS_WORKLOAD_ID", "local"))
    for task in tasks:
        events.task_started(phase, task["id"])
        started = time.monotonic()
        passed = check(task, answer(task))
        events.task_completed(
            phase,
            task["id"],
            outcome="passed" if passed else "failed",
            reward=1.0 if passed else 0.0,
            duration_ms=(time.monotonic() - started) * 1000,
        )
```

Phases are `baseline`, `training` and `evaluation`. To show a real change,
score the same held-out task IDs in `baseline` and in `evaluation`: a
comparison between different tasks is reported as not like-for-like. An event
the server would reject raises `nodus.EventValidationError` in your code,
before it is written.

Your code implements the trainer, model and task limit. A completed command
without task events has no reported RL score. Custom runs cannot claim managed
recipe validation.

## Use a prepared recipe

RL recipes describe supported evaluation and training runs. Recipe availability
reflects operator qualification and current launch settings. Admission and
capacity are checked again at launch, so an available recipe can still fail to
launch.

Start by finding the recipe you intend to use and checking its current status:

```python
import nodus

def find_recipe(client):
    recipes = client.rl.list_recipes()
    recipe = next(
        (
            item for item in recipes
            if item.id == "reasoning-gym-leg-counting"
        ),
        None,
    )

    if recipe is None:
        raise RuntimeError("The requested RL recipe is not offered")
    if not recipe.available:
        reason = recipe.unavailable_reason or "No reason was provided"
        raise RuntimeError(f"RL recipe is unavailable: {reason}")
    return recipe
```

Build the configuration explicitly, then ask the server to normalize and
review it. Preview does not launch a workload.

```python
def prepare_run(client, recipe):
    configuration = {
        "recipe_id": recipe.id,
        "recipe_version": recipe.version,
        "mode": "evaluate",
        "evaluation_tasks": 16,
        "training_steps": 20,
        "seed": 42,
        "include_traces": False,
    }

    preview = client.rl.preview(configuration)
    print(preview.estimate)
    print(preview.phases)
    print(preview.outputs)

    if not preview.launchable:
        raise RuntimeError(
            "RL run cannot launch: " + ", ".join(preview.blocking_reasons)
        )
    return preview
```

Inspect the normalized configuration, phases, outputs, and estimate before
launching. The server returns a review token for that exact preview. A changed
configuration needs a new preview and review token.

Launch only after your application or a person has accepted the preview. Use a
stable idempotency key for one intended run and store it with your own run
record.

```python
def launch_run(client, preview, stable_run_id):
    workload = client.rl.launch(
        configuration=preview.configuration,
        review_token=preview.review_token,
        idempotency_key=stable_run_id,
    )
    print(workload.id)
    return workload
```

`launch()` requires all three values. It does not select a recipe, fill in a
configuration, or generate an idempotency key. This makes the paid action
explicit and lets an application retry safely.

If a timeout or connection failure leaves the launch outcome unclear, retry
the same configuration and review token with the same idempotency key. A new
key can create another paid run. Reusing one key with a different request
raises `IdempotencyConflictError`.

Normal SDK exceptions apply:

```python
def launch_with_error_handling(client, preview, stable_run_id):
    try:
        return client.rl.launch(
            configuration=preview.configuration,
            review_token=preview.review_token,
            idempotency_key=stable_run_id,
        )
    except nodus.IdempotencyConflictError:
        raise RuntimeError("The idempotency key belongs to a different request")
    except nodus.NodusError as error:
        raise RuntimeError(f"RL launch failed: {error}") from error
```

The asynchronous client has the same flow:

```python
async def prepare_async(client, configuration):
    recipes = await client.rl.list_recipes()
    recipe = next(
        (item for item in recipes if item.id == configuration["recipe_id"]),
        None,
    )
    if recipe is None or not recipe.available:
        raise RuntimeError("The requested RL recipe is unavailable")
    preview = await client.rl.preview(configuration)
    if not preview.launchable:
        raise RuntimeError(
            "RL run cannot launch: " + ", ".join(preview.blocking_reasons)
        )
    return preview

async def launch_async(client, preview, stable_run_id):
    return await client.rl.launch(
        configuration=preview.configuration,
        review_token=preview.review_token,
        idempotency_key=stable_run_id,
    )
```

## Read task evidence and grading receipts

Use `client.rl.events()` for reported RL task evidence. `workload.events()`
returns separate workload lifecycle events.

```python
def read_available_tasks(client, workload_id, cursor=None):
    while True:
        page = client.rl.events(workload_id, after=cursor, limit=100)
        for row in page.events:
            event = row.event
            print(event.task_id, event.outcome, event.reward)
        if page.truncated or page.dropped_events:
            print("Task evidence is partial", page.dropped_events)
        cursor = page.next_cursor
        if not page.has_more:
            return cursor
```

Pass `next_cursor` unchanged as `after`. Keep the returned cursor for the next
read. A row ID is a separate string identity and must not be used as a cursor.
Recovery can repeat an application event identity in a later execution
generation. Keep each row and inspect its `generation` instead of deduplicating
across generations.
An empty page or `has_more=False` means no more rows were available in that
snapshot. Continue polling while the workload is active and read again after
observing its terminal status. Task events are application-reported evidence.
They do not independently prove model quality or workload completion.

For a workload with a server-admitted private grading plan, request receipts
for the explicit revision you submitted:

```python
def read_grading(client, workload_id, revision):
    results = client.rl.grading_results(workload_id, revision=revision)
    for receipt in results.receipts:
        print(receipt.task_id, receipt.state, receipt.reward)
    return results

async def read_evidence_async(client, workload_id, revision, cursor=None):
    page = await client.rl.events(workload_id, after=cursor)
    grading = await client.rl.grading_results(workload_id, revision=revision)
    return page, grading
```

Receipt rewards are `None` when absent and `0` for a measured zero. An
infrastructure failure does not supply a model reward. Cleanup fields describe
the grading attempts in the selected revision and do not establish parent
cleanup or final billing. A missing grading plan raises `NotFoundError`.
Reading receipts does not enable private grading for a workload.
