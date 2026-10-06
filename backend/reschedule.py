"""
Rescheduling — re-optimise a plan *from a freeze point onward*.

Unlike a fresh solve, a reschedule starts from a plan that is already being
executed on the shop floor:

* **completed** tasks stay exactly where they happened (history is frozen);
* **in-progress** tasks are pinned at their actual start, continued to the
  freeze point ``now``, and their remaining work is scheduled as a tail;
* **interrupted** tasks (a machine died mid-job) are cut at the freeze point
  and their remaining work becomes a regular not-started task;
* **pending** tasks are the only ones the solver is free to move.

Three kinds of disruption are folded in before the re-solve:

* resource *breakdowns* (extra unavailable calendar windows),
* *rush orders* (new tasks appended to the problem, which also bumps the
  problem version so the resulting plan stays reproducible),
* realised progress that differs from the plan (slower/faster tasks).

The tail is re-solved with a **stability-aware serial SGS**: every free task
is placed at the *nearest feasible slot to its previous start*, so tasks that
still fit keep their old slot and only the tasks forced by the disruption
move.  The other solvers can refine the tail; their candidate is only kept
when it beats the stable baseline on

    original_objective + stability_weight * sum |new_start - old_start|

so stability is an explicit, measurable quantity rather than a hope.  The
returned :class:`~backend.models.Solution` carries a ``change_summary`` with
the full per-task before/after diff ("what changed and by how much").
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import models, objectives
from .solvers import schedule_builder


class RescheduleError(ValueError):
    """Raised when the reported floor state cannot form a reschedule."""


# --------------------------------------------------------------------------- #
# Freeze-point derivation
# --------------------------------------------------------------------------- #

def derive_progress(problem: models.Problem,
                    baseline: models.Solution,
                    now: int) -> List[models.TaskProgress]:
    """Default field report at time ``now``: classify every baseline task as
    completed / in_progress / pending purely from its planned interval."""
    tmap = problem.task_map()
    base_starts = {a.task: a.start for a in baseline.assignments}
    out: List[models.TaskProgress] = []
    for tid in sorted(base_starts):
        s = base_starts[tid]
        d = tmap[tid].duration
        if s + d <= now:
            out.append(models.TaskProgress(task=tid, state="completed",
                                           actual_start=s, actual_end=s + d,
                                           processed=d))
        elif s < now < s + d:
            out.append(models.TaskProgress(task=tid, state="in_progress",
                                           actual_start=s, processed=now - s,
                                           remaining=d - (now - s)))
        else:
            out.append(models.TaskProgress(task=tid, state="pending"))
    # tasks absent from the baseline (e.g. unscheduled) are pending as well
    for t in problem.tasks:
        if t.id not in base_starts:
            out.append(models.TaskProgress(task=t.id, state="pending"))
    return out


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #

def _subtract_windows(avail: Optional[List[List[int]]],
                      block: Tuple[int, int]) -> Optional[List[List[int]]]:
    """Remove half-open ``block`` from availability calendar ``avail``
    (``None`` means always available)."""
    b0, b1 = block
    if avail is None:
        return [[-(10 ** 9), b0], [b1, 10 ** 9]]
    out: List[List[int]] = []
    for a0, a1 in avail:
        if b1 <= a0 or b0 >= a1:          # disjoint
            out.append([a0, a1])
            continue
        if b0 > a0:
            out.append([a0, b0])
        if b1 < a1:
            out.append([b1, a1])
    return out or [[0, 0]]


def _available_through(res: models.Resource, start: int, end: int) -> bool:
    """True if the resource is available on every slot of [start, end)."""
    return all(res.available_at(t) for t in range(start, end))


# --------------------------------------------------------------------------- #
# Frozen problem construction
# --------------------------------------------------------------------------- #

@dataclass
class FreezeInfo:
    """Bookkeeping produced alongside the frozen problem, used to stitch the
    full solution and the diff back together."""
    now: int
    frozen_starts: Dict[str, int]               # task id -> pinned start
    frozen_durations: Dict[str, int]            # task id -> pinned duration
    base_starts: Dict[str, int]                 # baseline starts (all tasks)
    base_ends: Dict[str, int]
    states: Dict[str, str]
    new_task_ids: List[str]
    interrupted: List[str]
    continued: List[str]
    breakdowns: List[models.Breakdown]
    duration_overrides: Dict[str, int]          # current problem duration
    original_durations: Dict[str, int]


def build_frozen_problem(
    problem: models.Problem,
    baseline: models.Solution,
    req: models.RescheduleRequest,
) -> Tuple[models.Problem, FreezeInfo]:
    """Return (problem_to_solve, bookkeeping).

    The returned problem shares the same id; the caller persists it (rush
    tasks are first-class problem edits).  Its free part, solved from the
    freeze point, is exactly the rescheduling problem.
    """
    now = req.now
    if now < 0:
        raise RescheduleError("freeze point 'now' must be >= 0")

    tmap_orig = problem.task_map()
    rmap = problem.resource_map()

    base_assign = {a.task: a for a in baseline.assignments}
    base_starts = {tid: a.start for tid, a in base_assign.items()}
    base_ends = {tid: a.end for tid, a in base_assign.items()}

    # -- 1. explicit progress overrides + defaults ------------------------ #
    progress: Dict[str, models.TaskProgress] = {}
    for pr in derive_progress(problem, baseline, now):
        progress[pr.task] = pr
    for pr in req.progress:
        if pr.task not in tmap_orig:
            raise RescheduleError(f"progress: unknown task '{pr.task}'")
        progress[pr.task] = pr

    # -- 2. deep-copy problem; rush tasks become permanent --------------- #
    frozen = copy.deepcopy(problem)
    new_ids: List[str] = []
    for rush in req.rush_tasks:
        if rush.id in frozen.task_map():
            raise RescheduleError(f"rush task id '{rush.id}' already exists")
        for r in rush.resource_requirements:
            if r not in rmap:
                raise RescheduleError(
                    f"rush task {rush.id}: unknown resource '{r}'")
        for dep in rush.dependencies:
            if dep not in tmap_orig and dep not in {r.id for r in req.rush_tasks}:
                raise RescheduleError(
                    f"rush task {rush.id}: unknown dependency '{dep}'")
        if rush.duration <= 0:
            raise RescheduleError(f"rush task {rush.id}: duration must be > 0")
        release = now if rush.release_time is None else rush.release_time
        frozen.tasks.append(models.Task(
            id=rush.id, name=rush.name, duration=rush.duration,
            resource_requirements=dict(rush.resource_requirements),
            dependencies=list(rush.dependencies),
            release_time=release, due_date=rush.due_date,
            weight=rush.weight, priority=rush.priority))
        new_ids.append(rush.id)
        progress[rush.id] = models.TaskProgress(task=rush.id, state="pending")

    ftmap = frozen.task_map()

    # -- 3. breakdown calendars ------------------------------------------- #
    breakdowns: List[models.Breakdown] = []
    for bd in req.breakdowns:
        if bd.resource not in rmap:
            raise RescheduleError(f"breakdown: unknown resource '{bd.resource}'")
        if bd.end <= now:
            # entirely in the past: irrelevant, ignore rather than fail
            continue
        breakdowns.append(bd)
        res = next(r for r in frozen.resources if r.id == bd.resource)
        res.availability = _subtract_windows(
            res.availability, (max(bd.start, now), bd.end))

    # -- 4. classify / pin every task ------------------------------------- #
    # frozen_starts holds ONLY history that may not move:
    #   completed tasks (actual interval) and continued in-progress tails
    #   (pinned to start exactly at `now`).
    frozen_starts: Dict[str, int] = {}
    frozen_durations: Dict[str, int] = {}
    states: Dict[str, str] = {t.id: "pending" for t in frozen.tasks}
    interrupted: List[str] = []
    continued: List[str] = []
    duration_overrides: Dict[str, int] = {}

    for tid, task in ftmap.items():
        pr = progress.get(tid)
        state = pr.state if pr else "pending"

        if tid in new_ids:
            # rush tasks: brand new, free
            states[tid] = "pending"
            continue

        if state == "pending":
            # history cannot schedule anything before the freeze point
            task.release_time = max(task.release_time, now)
            continue

        if tid not in base_assign:
            raise RescheduleError(
                f"task '{tid}' reported {state} but it has no baseline "
                f"assignment")

        if state == "completed":
            s = pr.actual_start if pr.actual_start is not None else base_starts[tid]
            e = pr.actual_end if pr.actual_end is not None else base_ends[tid]
            if e > now:
                raise RescheduleError(
                    f"task '{tid}' completed at {e} but freeze point is {now}")
            if e <= s:
                raise RescheduleError(
                    f"task '{tid}': completion {e} not after start {s}")
            states[tid] = "completed"
            frozen_starts[tid] = s
            frozen_durations[tid] = e - s
            task.duration = e - s
            duration_overrides[tid] = task.duration
            continue

        # in_progress or interrupted
        a_start = pr.actual_start if pr.actual_start is not None \
            else base_starts[tid]
        processed = pr.processed if pr.processed else max(0, now - a_start)
        if processed <= 0 or a_start >= now:
            if a_start > now and pr.processed > 0:
                raise RescheduleError(
                    f"task '{tid}' reported {state} with start {a_start} after "
                    f"the freeze point {now}")
            # no work consumed yet (includes a_start == now): plain pending
            states[tid] = "pending"
            task.release_time = max(task.release_time, now)
            continue

        remaining = pr.remaining
        if remaining is None:
            remaining = max(0, task.duration - processed)
        if remaining < 0:
            raise RescheduleError(
                f"task '{tid}': processed {processed} exceeds "
                f"duration {task.duration}")
        # already-consumed resources over [a_start, a_start+processed)
        if not all(_available_through(rmap[rid], a_start, a_start + processed)
                   for rid in task.resource_requirements if rid in rmap):
            raise RescheduleError(
                f"task '{tid}' ran on a resource that was unavailable during "
                f"[{a_start}, {a_start + processed}); mark it interrupted "
                f"before the breakdown instead")

        task.duration = remaining
        duration_overrides[tid] = remaining

        if remaining == 0:
            # finished exactly at (or effectively by) the freeze point
            states[tid] = "completed"
            frozen_starts[tid] = a_start
            frozen_durations[tid] = processed
            task.duration = processed
            duration_overrides[tid] = processed
            continue

        if state == "in_progress":
            # continuing tail must be placeable at exactly `now`
            if not all(_available_through(
                    next(r for r in frozen.resources if r.id == rid),
                    now, now + remaining)
                    for rid in task.resource_requirements if rid in rmap):
                raise RescheduleError(
                    f"task '{tid}' cannot continue at t={now}: a required "
                    f"resource is unavailable; mark the task interrupted "
                    f"instead")
            states[tid] = "in_progress"
            frozen_starts[tid] = now
            frozen_durations[tid] = remaining
            continued.append(tid)
        else:
            states[tid] = "interrupted"
            interrupted.append(tid)

    # -- 5. hard constraints: pin frozen tasks, prune stale ones ---------- #
    kept_hc: List[models.HardConstraint] = []
    for c in frozen.hard_constraints:
        p = c.params
        tid = p.get("task")
        if c.type == "fixed_start" and tid in ftmap:
            if states[tid] == "pending" and p.get("start") is not None \
                    and p["start"] < now:
                continue  # unenforceable past fixed start -> drop
            kept_hc.append(c)
        elif c.type == "time_window" and tid in ftmap:
            if states[tid] == "pending" and p.get("deadline") is not None \
                    and p["deadline"] < now:
                raise RescheduleError(
                    f"task '{tid}' has a hard deadline {p['deadline']} "
                    f"already past at freeze point {now}")
            kept_hc.append(c)
        else:
            kept_hc.append(c)
    frozen.hard_constraints = kept_hc
    for tid in frozen_starts:
        frozen.hard_constraints.append(models.HardConstraint(
            id=models.new_id("hc_pin"), type="fixed_start",
            params={"task": tid, "start": frozen_starts[tid]}))

    # -- 6. stability term: preferred-start soft constraint on free tasks  #
    w = float(req.stability_weight)
    for tid, task in ftmap.items():
        if states[tid] != "pending":
            continue
        pref = base_starts.get(tid)
        if pref is None:
            pref = task.release_time
        pref = max(pref, now)
        frozen.soft_constraints.append(models.SoftConstraint(
            id=models.new_id("sc_stab"), type="preferred_start",
            params={"task": tid, "start": pref,
                    "new": tid in new_ids},
            penalty=0.0 if tid in new_ids else w, factor=1.0))

    errors = models.validate_problem(frozen)
    if errors:
        raise RescheduleError("frozen problem is invalid: " + "; ".join(errors))

    info = FreezeInfo(
        now=now,
        frozen_starts=frozen_starts,
        frozen_durations=frozen_durations,
        base_starts=dict(base_starts),
        base_ends=dict(base_ends),
        states=states,
        new_task_ids=new_ids,
        interrupted=interrupted,
        continued=continued,
        breakdowns=breakdowns,
        duration_overrides=duration_overrides,
        original_durations={t.id: t.duration for t in problem.tasks},
    )
    return frozen, info


# --------------------------------------------------------------------------- #
# Stable serial SGS (minimum-perturbation decoder)
# --------------------------------------------------------------------------- #

def stable_decode(problem: models.Problem,
                  frozen_starts: Dict[str, int],
                  preferred: Dict[str, int],
                  now: int) -> Dict[str, int]:
    """Serial SGS with a *nearest-to-preferred* placement rule.

    Tasks already pinned (completed / in-progress tails) are pre-loaded and
    never moved; each free task is placed at the feasible slot with smallest
    ``|start - preferred|`` (ties resolved toward the earlier slot).  With
    preferred == baseline start this leaves every task that still fits in its
    old slot and moves only what the disruption forces.
    """
    starts = dict(frozen_starts)
    tmap = problem.task_map()
    new_ids = {
        t for c in problem.soft_constraints
        if c.type == "preferred_start" and c.params.get("new")
        for t in (c.params.get("task"),) if t}
    # Order available tasks by preferred start (earliest first); the two-pass
    # placement below (established before rush) does the rest.
    order_keys = {tid: -float(preferred.get(tid, tmap[tid].release_time))
                  for tid in tmap}
    order = schedule_builder.topological_order(problem, order_keys)
    free = [tid for tid in order if tid not in frozen_starts]

    def nearest(tid: str) -> Optional[int]:
        task = tmap[tid]
        d = task.duration
        earliest = max(task.release_time, now)
        for dep in task.dependencies:
            if dep in starts:
                earliest = max(earliest, starts[dep] + tmap[dep].duration)
        for c in problem.hard_constraints:
            if c.type == "precedence" and c.params.get("after") == tid:
                b = c.params.get("before")
                if b in starts:
                    earliest = max(earliest,
                                   starts[b] + tmap[b].duration)
        for c in problem.hard_constraints:
            p = c.params
            if p.get("task") != tid:
                continue
            if c.type == "time_window" and p.get("release") is not None:
                earliest = max(earliest, p["release"])
            if c.type == "fixed_start" and p.get("start") is not None:
                s = p["start"]
                if (s >= earliest and s + d <= problem.horizon
                        and schedule_builder._window_ok(
                            problem, task, s, starts, problem.resource_map())):
                    return s
                return None

        pref = preferred.get(tid, earliest)
        rmap = problem.resource_map()
        best: Optional[int] = None
        best_key: Optional[Tuple[int, int]] = None
        for t in range(earliest, problem.horizon - d + 1):
            if not schedule_builder._window_ok(problem, task, t, starts, rmap):
                continue
            if not schedule_builder._window_deadline_ok(problem, tid, t):
                continue
            # nearest slot to the preferred start; ties resolve to the
            # earlier slot so urgent work still starts promptly
            key = (abs(t - pref), t)
            if best_key is None or key < best_key:
                best, best_key = t, key
            if t >= pref and (t - pref) >= (best_key[0] if best_key else 0):
                break  # past the preferred point distances only grow
        return best

    # Two passes: established tasks claim their old slots first, then rush
    # orders are fit into whatever capacity remains.
    established = [tid for tid in free if tid not in new_ids]
    rush = [tid for tid in free if tid in new_ids]
    for tid in established + rush:
        s = nearest(tid)
        if s is not None:
            starts[tid] = s
    return starts


def _fixed_tasks(problem: models.Problem) -> set:
    """Tasks with a hard fixed_start constraint (they may not be re-placed)."""
    return {c.params["task"] for c in problem.hard_constraints
            if c.type == "fixed_start" and c.params.get("task")}


def _polish(problem: models.Problem, starts: Dict[str, int],
            preferred: Dict[str, int], frozen: set,
            passes: int = 2, now: int = 0) -> Dict[str, int]:
    """Coordinate-descent polish: re-place one free task at a time (others
    fixed) at the slot with the smallest deviation from its preferred start,
    keeping every change that reduces deviation without breaking feasibility.
    """
    tmap = problem.task_map()
    rmap = problem.resource_map()
    order = schedule_builder.topological_order(problem)
    pinned = frozen | _fixed_tasks(problem)
    free = [t for t in order if t not in pinned]
    current = dict(starts)

    def pred_earliest(tid: str, others: Dict[str, int]) -> int:
        task = tmap[tid]
        e = max(task.release_time, now)
        for dep in task.dependencies:
            if dep in others:
                e = max(e, others[dep] + tmap[dep].duration)
        for c in problem.hard_constraints:
            if c.type == "precedence" and c.params.get("after") == tid:
                b = c.params.get("before")
                if b in others:
                    e = max(e, others[b] + tmap[b].duration)
        return e

    for _ in range(passes):
        improved = False
        for tid in free:
            if tid not in current:
                continue
            task = tmap[tid]
            d = task.duration
            others = {k: v for k, v in current.items() if k != tid}
            old = current[tid]
            pref = preferred.get(tid, old)
            candidate = old
            candidate_key = abs(old - pref)
            earliest = pred_earliest(tid, others)
            for t in range(earliest, problem.horizon - d + 1):
                if abs(t - pref) >= candidate_key:
                    continue
                if schedule_builder._window_ok(problem, task, t, others, rmap) \
                        and schedule_builder._window_deadline_ok(problem, tid, t):
                    candidate, candidate_key = t, abs(t - pref)
            if candidate != old:
                current[tid] = candidate
                improved = True
        if not improved:
            break
    return current


def stability_objective(problem: models.Problem,
                        starts: Dict[str, int]) -> float:
    """Objective including injected preferred-start penalties and a large
    infeasibility penalty per unplaced task."""
    from .solvers.base import INFEASIBILITY_PENALTY
    soft = objectives.soft_penalty(problem, starts)
    obj = objectives.evaluate_objective(problem, starts, soft)
    missing = len(problem.tasks) - len(starts)
    return obj + INFEASIBILITY_PENALTY * missing


# --------------------------------------------------------------------------- #
# Change diff
# --------------------------------------------------------------------------- #

def build_change_summary(problem: models.Problem,
                         starts: Dict[str, int],
                         info: FreezeInfo,
                         base_objective: Optional[float],
                         new_objective: Optional[float]) -> Dict[str, Any]:
    """Per-task before/after rows plus aggregate change statistics."""
    tmap = problem.task_map()
    rows: List[Dict[str, Any]] = []
    n_moved = 0
    n_new = 0
    total_shift = 0
    abs_shift = 0
    max_shift = 0

    for tid in sorted(tmap):
        state = info.states.get(tid, "pending")
        new_start = starts.get(tid)
        base_start = info.base_starts.get(tid)
        base_end = info.base_ends.get(tid)
        cur_dur = tmap[tid].duration
        new_end = (new_start + cur_dur) if new_start is not None else None

        if tid in info.new_task_ids:
            change = "added"
            n_new += 1
            delta = None
        elif state in ("completed",):
            change = "unchanged"
            delta = 0
        elif state in ("in_progress",) and tid in info.continued:
            # tail rescheduled: compare tail start to the original plan end
            # of consumed portion (== now) — display its new window
            change = "continued"
            delta = (new_start - info.now) if new_start is not None else None
        elif state == "interrupted":
            change = "rescheduled"
            delta = (new_start - info.now) if new_start is not None else None
            if new_start is not None:
                n_moved += 1
        elif base_start is None:
            change = "scheduled" if new_start is not None else "unscheduled"
            delta = None
        elif new_start is None:
            change = "unscheduled"
            delta = None
            n_moved += 1
        elif new_start != base_start:
            change = "moved"
            delta = new_start - base_start
            n_moved += 1
            total_shift += delta
            abs_shift += abs(delta)
            max_shift = max(max_shift, abs(delta))
        else:
            change = "unchanged"
            delta = 0

        rows.append({
            "task": tid,
            "name": tmap[tid].name,
            "state": state,
            "change": change,
            "base_start": base_start,
            "base_end": base_end,
            "new_start": new_start,
            "new_end": new_end,
            "delta": delta,
            "duration": cur_dur,
            "original_duration": info.original_durations.get(tid),
        })

    summary = {
        "now": info.now,
        "n_total": len(tmap),
        "n_completed": sum(1 for s in info.states.values() if s == "completed"),
        "n_in_progress": sum(1 for s in info.states.values()
                             if s == "in_progress"),
        "n_interrupted": len(info.interrupted),
        "n_pending": sum(1 for s in info.states.values() if s == "pending"),
        "n_moved": n_moved,
        "n_new": n_new,
        "total_shift": total_shift,
        "abs_shift": abs_shift,
        "max_shift": max_shift,
        "n_breakdowns": len(info.breakdowns),
        "breakdowns": [bd.to_dict() for bd in info.breakdowns],
        "rush_tasks": list(info.new_task_ids),
        "base_objective": base_objective,
        "new_objective": new_objective,
        "objective_delta": (round(new_objective - base_objective, 4)
                            if base_objective is not None
                            and new_objective is not None else None),
        "rows": rows,
    }
    return summary


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def _append_rush_tasks(problem: models.Problem,
                       req: models.RescheduleRequest) -> models.Problem:
    """Return a copy of ``problem`` with rush orders appended as first-class
    tasks.  Nothing else from the freeze transformation (pinned constraints,
    shortened in-progress durations, breakdown calendars) is persisted: those
    describe this one reschedule event, not the problem definition."""
    updated = copy.deepcopy(problem)
    for rush in req.rush_tasks:
        updated.tasks.append(models.Task(
            id=rush.id, name=rush.name, duration=rush.duration,
            resource_requirements=dict(rush.resource_requirements),
            dependencies=list(rush.dependencies),
            release_time=rush.release_time if rush.release_time is not None
            else req.now,
            due_date=rush.due_date, weight=rush.weight, priority=rush.priority))
    return updated


def reschedule(problem: models.Problem,
               baseline: models.Solution,
               req: models.RescheduleRequest,
               solver_factory=None) -> Tuple[models.Problem, models.Solution]:
    """Execute a full reschedule.

    Returns ``(updated_problem, new_solution)``.  ``updated_problem`` is the
    original problem with rush orders appended (the storage layer versions it
    on save); progress, breakdowns and injected stability terms are kept in
    the ephemeral solve model only.
    """
    from .solvers.base import get_solver

    t0 = time.time()
    frozen_problem, info = build_frozen_problem(problem, baseline, req)
    preferred: Dict[str, int] = {}
    for tid in frozen_problem.task_map():
        if info.states[tid] != "pending":
            continue
        pref = info.base_starts.get(tid)
        if pref is None:
            pref = frozen_problem.task_map()[tid].release_time
        preferred[tid] = max(pref, info.now)

    # -- stable baseline: minimum perturbation ---------------------------- #
    stable_starts = stable_decode(
        frozen_problem, info.frozen_starts, preferred, info.now)
    stable_starts = _polish(frozen_problem, stable_starts, preferred,
                            set(info.frozen_starts), now=info.now)
    best_starts = stable_starts
    best_obj = stability_objective(frozen_problem, best_starts)
    method = "stable-sgs"
    refinement: Dict[str, Any] = {}

    # -- optional refinement by a regular solver -------------------------- #
    # Every pinned task (completed history + continuing in-progress tails)
    # is pre-seeded in the candidate so the decoder's capacity/concurrent
    # checks always see the frozen prefix; a solver can neither move nor
    # accidentally "re-place" those tasks.
    if req.solver and req.solver != "stable":
        try:
            factory = solver_factory or get_solver
            sol2 = factory(req.solver).solve(frozen_problem, req.params)
        except ValueError as exc:
            raise RescheduleError(f"solver '{req.solver}' failed: {exc}")
        cand = {a.task: a.start for a in sol2.assignments
                if a.task not in info.frozen_starts}
        cand.update(info.frozen_starts)
        cand_obj = stability_objective(frozen_problem, cand)
        n_placed = len(cand)
        refinement = {"solver": req.solver,
                      "raw_objective": sol2.objective_value,
                      "accepted": False}
        # never trade away feasibility or task coverage for objective quality
        if n_placed == len(frozen_problem.tasks) and \
                not objectives.hard_violations(frozen_problem, cand) and \
                cand_obj < best_obj - 1e-9:
            best_starts, best_obj, method = cand, cand_obj, req.solver
            refinement["accepted"] = True

    feasible = len(best_starts) == len(frozen_problem.tasks)
    base_obj_value = baseline.objective_value

    # objective of the new schedule under the *original* objective (i.e.
    # excluding the injected stability penalties), for honest comparison
    orig_obj = objectives.evaluate_objective(
        frozen_problem, best_starts,
        objectives.soft_penalty(_strip_stability(frozen_problem), best_starts))

    from .solvers.schedule_builder import schedule_to_assignments
    assignments = schedule_to_assignments(frozen_problem, best_starts)
    # carry shortened durations (in-progress tails, interrupted remainders)
    # onto the assignments, since the persisted problem keeps full durations
    for a in assignments:
        orig_dur = info.original_durations.get(a.task)
        if orig_dur is not None and a.end - a.start != orig_dur:
            a.duration_override = a.end - a.start
    ms = objectives.makespan(frozen_problem, best_starts)
    violations = objectives.hard_violations(frozen_problem, best_starts)

    summary = build_change_summary(
        frozen_problem, best_starts, info, base_obj_value, round(orig_obj, 4))
    summary["method"] = method
    summary["refinement"] = refinement
    summary["stability_weight"] = req.stability_weight

    sol = models.Solution(
        id=models.new_id("sol"),
        problem_id=problem.id,
        solver="stable" if method == "stable-sgs" else method,
        status=("feasible" if feasible and not violations else "infeasible"),
        objective_value=round(orig_obj, 4),
        makespan=ms,
        assignments=assignments,
        metrics=objectives.compute_metrics(frozen_problem, best_starts),
        params={"reschedule": True, "method": method,
                "stability_weight": req.stability_weight,
                "solver_requested": req.solver, **req.params},
        solve_time=round(time.time() - t0, 4),
        message=(f"rescheduled from t={info.now}: "
                 f"{summary['n_moved']} moved, {summary['n_new']} added, "
                 f"{summary['n_interrupted']} interrupted"),
        baseline_solution_id=baseline.id,
        rescheduled_at=info.now,
        change_summary=summary,
    )
    updated_problem = _append_rush_tasks(problem, req)
    return updated_problem, sol


def _strip_stability(problem: models.Problem) -> models.Problem:
    """Shallow copy with the injected preferred_start constraints removed,
    so the reported objective reflects the original business objective."""
    p = copy.copy(problem)
    p.soft_constraints = [
        c for c in problem.soft_constraints
        if not (c.type == "preferred_start"
                and c.id.startswith("sc_stab"))]
    return p
