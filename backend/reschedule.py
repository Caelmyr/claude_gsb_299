"""
Roll-forward rescheduling ("从现在这个时点往后重排").

When execution deviates from the published plan (a task is late, a machine
breaks, a rush order arrives) the shop floor cannot re-run the whole plan:
everything already done is immutable.  This module implements the standard
*frozen-prefix / solved-suffix* scheme:

1. **Classify** every task at time ``now`` from the reported execution state:
   - ``completed``    -- frozen; its actual interval is kept verbatim.
   - ``in_progress``  -- frozen in the past; only the *remaining* work is
                         modelled, fixed to continue from ``now``.
   - not started      -- free; only these tasks may move.
2. **Build the suffix problem** on ``[now, horizon)``: frozen tasks become
   fixed-start entries, breakdowns shrink resource calendars, rush orders are
   appended as ordinary tasks, and precedence from finished predecessors is
   folded into release times.
3. **Solve the suffix** with a minimum-displacement strategy (``stability``:
   keep each free task at its baseline start whenever it is still feasible) or
   one of the regular solvers with a quadratic-ish *stability penalty*
   (soft preferred-window pinned at the baseline start).
4. **Merge** the frozen prefix with the solved suffix and emit a per-task
   :class:`~backend.models.ScheduleChange` diff plus disruption summaries, so
   the user can see exactly what moved and by how much.

The module is pure orchestration over :mod:`models`, :mod:`objectives` and the
existing SGS decoder / solver registry; it contains no I/O.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional, Tuple

from . import models, objectives
from .solvers import base as solver_base
from .solvers import schedule_builder

# Weight of the "stay at the baseline start" soft term used when a regular
# (non-stability) solver re-optimises the suffix.  Large by design: feasibility
# and the original objective trade off against disruption, but displacement is
# meant to hurt.
DEFAULT_STABILITY_WEIGHT = 10.0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _clone(problem: models.Problem) -> models.Problem:
    return models.Problem.from_dict(copy.deepcopy(problem.to_dict()))


def _baseline_maps(baseline: Optional[models.Solution]
                   ) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, List[str]]]:
    starts: Dict[str, int] = {}
    ends: Dict[str, int] = {}
    resources: Dict[str, List[str]] = {}
    if baseline is not None:
        for a in baseline.assignments:
            starts[a.task] = a.start
            ends[a.task] = a.end
            resources[a.task] = list(a.resources)
    return starts, ends, resources


def _subtract_interval(avail: List[List[int]], ds: int, de: int) -> List[List[int]]:
    """Subtract half-open ``[ds, de)`` from a union of availability intervals."""
    out: List[List[int]] = []
    for a, b in avail:
        if de <= a or ds >= b:
            out.append([a, b])
            continue
        if ds <= a and de >= b:
            continue  # fully covered
        if ds > a:
            out.append([a, min(b, ds)])
        if de < b and de > a:
            out.append([max(a, de), b])
    return out


def _clamp_intervals(intervals: List[List[int]], horizon: int) -> List[List[int]]:
    out = []
    for a, b in intervals:
        a, b = max(0, a), min(horizon, b)
        if b > a:
            out.append([a, b])
    return out


# --------------------------------------------------------------------------- #
# Validation / state normalisation
# --------------------------------------------------------------------------- #

def _normalise_state(problem: models.Problem,
                     baseline: Optional[models.Solution],
                     now: int,
                     progress: List[models.TaskProgress],
                     downtimes: List[models.ResourceDowntime],
                     new_tasks: List[models.Task]
                     ) -> Tuple[Dict[str, models.TaskProgress],
                                Dict[str, Tuple[int, int, List[str]]],
                                Dict[str, Tuple[int, List[str], int]],
                                List[str], Dict[str, int]]:
    """Validate the request and derive concrete per-task frozen data.

    Returns ``(progress_map, completed, in_progress, free_ids, actual_end)``
    where

    * completed[task]   = (actual_start, actual_end, resources)
    * in_progress[task] = (actual_start, resources, remaining), remainder runs
                          on ``[now, now + remaining)``.
    """
    if not isinstance(now, int) or now < 0:
        raise ValueError("now 必须是非负整数")
    tmap = problem.task_map()
    rids = {r.id for r in problem.resources}
    b_start, b_end, b_res = _baseline_maps(baseline)

    # -- resource downtimes ---------------------------------------------- #
    for d in downtimes:
        if d.resource not in rids:
            raise ValueError(f"停机资源不存在：{d.resource}")
        if d.start < 0:
            raise ValueError(f"停机开始时间不能为负：{d.resource}")
        if d.end is not None and d.end <= d.start:
            raise ValueError(f"停机区间无效：{d.resource} [{d.start}, {d.end})")

    # -- new rush-order tasks -------------------------------------------- #
    new_ids = {t.id for t in new_tasks}
    dup = new_ids & set(tmap)
    if dup:
        raise ValueError(f"急单任务 id 与现有任务冲突：{sorted(dup)}")

    # -- per-task progress ----------------------------------------------- #
    pmap: Dict[str, models.TaskProgress] = {}
    for p in progress:
        if p.task in pmap:
            raise ValueError(f"任务 {p.task} 的进度重复上报")
        if p.task not in tmap:
            raise ValueError(f"进度上报了不存在的任务：{p.task}")
        pmap[p.task] = p

    completed: Dict[str, Tuple[int, int, List[str]]] = {}
    inprog: Dict[str, Tuple[int, List[str], int]] = {}
    actual_end: Dict[str, int] = {}

    for tid, p in pmap.items():
        task = tmap[tid]
        res = p.resources if p.resources is not None else b_res.get(
            tid, sorted(task.resource_requirements))

        if p.status == "completed":
            end = p.actual_end if p.actual_end is not None else b_end.get(tid)
            if end is None:
                raise ValueError(f"已完成任务 {tid} 缺少实际完工时间（且无基线可推断）")
            start = p.actual_start if p.actual_start is not None else b_start.get(
                tid, end - task.duration)
            if start < 0 or end < start or end > now:
                raise ValueError(
                    f"已完成任务 {tid} 的实际区间 [{start}, {end}) 无效（必须在 now={now} 之前结束）")
            completed[tid] = (start, end, res)
            actual_end[tid] = end
        else:  # in_progress
            remaining = p.remaining
            if remaining is None:
                raise ValueError(f"进行中任务 {tid} 必须提供剩余工期 remaining")
            if remaining <= 0:
                raise ValueError(f"进行中任务 {tid} 的剩余工期必须为正（若已完工请上报 completed）")
            if remaining > task.duration:
                raise ValueError(
                    f"进行中任务 {tid} 剩余工期 {remaining} 超过总工期 {task.duration}")
            # remainder fixed from now; back out the actual start so the full
            # actual span matches the original duration when data is consistent
            start = p.actual_start if p.actual_start is not None else now - (
                task.duration - remaining)
            if start < 0:
                raise ValueError(f"进行中任务 {tid} 推算出的实际开工时间早于 0")
            inprog[tid] = (start, res, remaining)
            actual_end[tid] = now + remaining

    # -- sanity: a running/finished task's predecessors must be done ------ #
    state = {**{t: "completed" for t in completed},
             **{t: "in_progress" for t in inprog}}
    for b, a in problem.precedence_edges():
        if a in state and b not in actual_end:
            raise ValueError(
                f"任务 {a} 已{state[a] == 'completed' and '完工' or '开工'}，"
                f"但其前序 {b} 尚未完成，现场状态与依赖矛盾")
        if a in state and b in inprog and actual_end[b] > now:
            raise ValueError(
                f"任务 {a} 已开工，但其前序 {b} 仍在制（将于 {actual_end[b]} 完工），"
                f"现场状态与依赖矛盾")

    free_ids = [t.id for t in problem.tasks
                if t.id not in completed and t.id not in inprog]

    # -- machines needed by running work must actually be up at now ------ #
    for tid, (_, res, remaining) in inprog.items():
        task = tmap[tid]
        for r in res:
            for d in downtimes:
                if d.resource != r:
                    continue
                dend = d.end if d.end is not None else now + remaining
                if d.start < now + remaining and dend > now:
                    raise ValueError(
                        f"进行中任务 {tid} 仍需资源 {r}，但该资源在 "
                        f"[{d.start}, {d.end if d.end is not None else '∞'}) 停机")

    # -- running work must not overload a resource by itself -------------- #
    cap_of = {r.id: r.capacity for r in problem.resources}
    for c in problem.hard_constraints:
        if c.type == "resource_capacity":
            cap_of[c.params.get("resource")] = float(c.params.get("capacity", 1))
    load: Dict[Tuple[str, int], float] = {}
    for tid, (_, res, remaining) in inprog.items():
        task = tmap[tid]
        for r in res:
            amount = task.resource_requirements.get(r, 0.0)
            for slot in range(now, now + remaining):
                load[(r, slot)] = load.get((r, slot), 0.0) + amount
    for (r, slot), used in load.items():
        if used > cap_of.get(r, used) + 1e-9:
            raise ValueError(
                f"现场在制任务在 t={slot} 超用资源 {r}：占用 {used} > 容量 {cap_of.get(r)}")

    return pmap, completed, inprog, free_ids, actual_end


# --------------------------------------------------------------------------- #
# Suffix problem construction
# --------------------------------------------------------------------------- #

def _suffix_horizon(problem: models.Problem, now: int,
                    downtimes: List[models.ResourceDowntime],
                    new_tasks: List[models.Task]) -> int:
    """Extend the horizon so breakdowns and rush orders still fit.  The
    extension equals the *additional* unavailable time plus rush work slack."""
    extra = 0
    for d in downtimes:
        end = d.end if d.end is not None else problem.horizon
        extra += max(0, end - max(d.start, now))
    if new_tasks:
        extra += max(t.duration for t in new_tasks)
    return problem.horizon + max(0, extra)


def _suffix_problem(problem: models.Problem,
                    now: int,
                    free_ids: List[str],
                    completed: Dict[str, Tuple[int, int, List[str]]],
                    inprog: Dict[str, Tuple[int, List[str], int]],
                    actual_end: Dict[str, int],
                    downtimes: List[models.ResourceDowntime],
                    new_tasks: List[models.Task],
                    b_start: Dict[str, int],
                    stability_weight: float,
                    horizon: int) -> models.Problem:
    tmap_orig = problem.task_map()

    # resource calendars minus breakdown windows
    resources: List[models.Resource] = []
    for r in problem.resources:
        nr = models.Resource.from_dict(copy.deepcopy(r.to_dict()))
        intervals = nr.availability if nr.availability is not None else [[0, horizon]]
        for d in downtimes:
            if d.resource == nr.id:
                dend = d.end if d.end is not None else horizon
                intervals = _subtract_interval(intervals, max(d.start, now), dend)
        nr.availability = _clamp_intervals(intervals, horizon)
        resources.append(nr)

    suffix_ids = set(free_ids) | set(inprog) | {t.id for t in new_tasks}

    # release-time floor inherited from finished predecessors
    folded_release: Dict[str, int] = {}

    def fold_edge(before: str, after: str) -> None:
        if after in suffix_ids and before in actual_end:
            folded_release[after] = max(folded_release.get(after, 0),
                                        actual_end[before])

    for t in problem.tasks:
        for dep in t.dependencies:
            fold_edge(dep, t.id)
    for c in problem.hard_constraints:
        if c.type == "precedence":
            fold_edge(c.params.get("before"), c.params.get("after"))

    tasks: List[models.Task] = []

    # in-progress work -> fixed-start short remainder task, same id
    for tid, (_, res, remaining) in inprog.items():
        orig = tmap_orig[tid]
        req = {r: orig.resource_requirements[r] for r in res
               if r in orig.resource_requirements}
        if not req:
            req = dict(orig.resource_requirements)
        tasks.append(models.Task(
            id=tid, name=orig.name, duration=remaining,
            resource_requirements=req,
            dependencies=[dep for dep in orig.dependencies if dep in suffix_ids],
            release_time=max(now, folded_release.get(tid, 0)),
            due_date=orig.due_date, weight=orig.weight, priority=orig.priority))

    # untouched tasks: deps to finished work are folded into release times
    for tid in free_ids:
        orig = tmap_orig[tid]
        rel = max(now, orig.release_time, folded_release.get(tid, 0))
        tasks.append(models.Task(
            id=tid, name=orig.name, duration=orig.duration,
            resource_requirements=dict(orig.resource_requirements),
            dependencies=[dep for dep in orig.dependencies if dep in suffix_ids],
            release_time=rel, due_date=orig.due_date,
            weight=orig.weight, priority=orig.priority))

    # rush orders
    for nt in new_tasks:
        rel = max(now, nt.release_time, folded_release.get(nt.id, 0))
        tasks.append(models.Task(
            id=nt.id, name=nt.name, duration=nt.duration,
            resource_requirements=dict(nt.resource_requirements),
            dependencies=[dep for dep in nt.dependencies if dep in suffix_ids],
            release_time=rel, due_date=nt.due_date,
            weight=nt.weight, priority=nt.priority))

    # hard constraints: drop anything that only touches frozen/completed work
    hard: List[models.HardConstraint] = []
    for c in problem.hard_constraints:
        p = c.params
        if c.type == "precedence":
            if p.get("before") in suffix_ids and p.get("after") in suffix_ids:
                hard.append(models.HardConstraint(id=c.id, type=c.type, params=dict(p)))
        elif c.type == "time_window":
            if p.get("task") in suffix_ids:
                hard.append(models.HardConstraint(id=c.id, type=c.type, params=dict(p)))
        elif c.type == "fixed_start":
            if p.get("task") in free_ids:
                hard.append(models.HardConstraint(id=c.id, type=c.type, params=dict(p)))
            # fixed_start on an in-progress task is replaced by the now-anchor
        elif c.type == "non_overlap":
            group = [t for t in p.get("tasks", []) if t in suffix_ids]
            if len(group) >= 2:
                np = dict(p)
                np["tasks"] = group
                hard.append(models.HardConstraint(id=c.id, type=c.type, params=np))
        elif c.type in ("resource_capacity", "max_concurrent"):
            hard.append(models.HardConstraint(id=c.id, type=c.type, params=dict(p)))
        elif c.type == "resource_assignment":
            if p.get("task") in suffix_ids:
                hard.append(models.HardConstraint(id=c.id, type=c.type, params=dict(p)))

    # every in-progress remainder is pinned to start exactly at now
    for i, tid in enumerate(inprog):
        hard.append(models.HardConstraint(
            id=f"__rs_fixed_{i}", type="fixed_start",
            params={"task": tid, "start": now}))

    # soft constraints: keep those still referencing suffix tasks
    soft: List[models.SoftConstraint] = []
    for c in problem.soft_constraints:
        p = c.params
        t = p.get("task")
        if c.type in ("due_date", "preferred_window"):
            if t in suffix_ids:
                soft.append(_copy_soft(c, p))
        elif c.type == "min_gap":
            if p.get("a") in suffix_ids and p.get("b") in suffix_ids:
                soft.append(_copy_soft(c, p))
        else:
            soft.append(_copy_soft(c, p))

    # stability terms: pin each *existing free* task to its baseline start.
    # Rush orders have no baseline and move freely.
    for i, tid in enumerate(free_ids):
        anchor = b_start.get(tid)
        if anchor is None:
            continue
        soft.append(models.SoftConstraint(
            id=f"__rs_stab_{i}", type="preferred_window",
            params={"task": tid, "start": max(now, anchor), "end": max(now, anchor)},
            penalty=float(stability_weight), factor=1.0))

    suffix = models.Problem(
        id=problem.id, name=f"{problem.name}（滚动重排后缀 @ {now}）",
        description=problem.description, horizon=horizon,
        time_unit=problem.time_unit, resources=resources, tasks=tasks,
        hard_constraints=hard, soft_constraints=soft,
        objective=models.Objective.from_dict(copy.deepcopy(problem.objective.to_dict())))

    errors = models.validate_problem(suffix)
    if errors:
        raise ValueError("后缀问题校验失败：" + "; ".join(errors[:5]))
    return suffix


def _copy_soft(c: models.SoftConstraint, params: Dict[str, Any]) -> models.SoftConstraint:
    return models.SoftConstraint(id=c.id, type=c.type, params=dict(params),
                                 penalty=c.penalty, factor=c.factor)


# --------------------------------------------------------------------------- #
# Solving the suffix
# --------------------------------------------------------------------------- #

def _solve_suffix(suffix: models.Problem, solver_name: str,
                  params: Dict[str, Any], free_ids: List[str],
                  inprog: Dict[str, Tuple[int, List[str], int]],
                  b_start: Dict[str, int], now: int
                  ) -> Tuple[Dict[str, int], float, str, Dict[str, Any]]:
    """Return ``(starts, elapsed, message, info)`` for the suffix."""
    fixed_starts = {tid: now for tid in inprog}
    t0 = time.time()

    if solver_name == "stability":
        # baseline order (earlier planned start first); tasks with no baseline
        # slot (rush orders, tasks the baseline could not place) go last.
        order: Dict[str, float] = {}
        for t in suffix.tasks:
            bs = b_start.get(t.id)
            order[t.id] = float(-bs) if bs is not None else float(-suffix.horizon - 1)
        starts = schedule_builder.decode(
            suffix, priorities=order, fixed_starts=fixed_starts,
            anchor_starts={tid: b_start[tid] for tid in free_ids if tid in b_start})
        return starts, time.time() - t0, "锚定原计划的最少改动串行 SGS", {}

    if solver_name == "greedy":
        starts = schedule_builder.decode(
            suffix, priorities=schedule_builder.greedy_order(suffix),
            fixed_starts=fixed_starts)
        return starts, time.time() - t0, "从 now 起的优先规则重排", {}

    # meta-heuristics: fixed_start hard constraints pin the in-progress work,
    # injected preferred-window soft terms penalise displacement.
    solver = solver_base.get_solver(solver_name)
    sol = solver.solve(suffix, params)
    starts = {a.task: a.start for a in sol.assignments}
    for tid, s in fixed_starts.items():
        starts.setdefault(tid, s)
    info = {"suffix_solver_status": sol.status, "suffix_solver_message": sol.message,
            "suffix_objective_value": sol.objective_value}
    return starts, time.time() - t0, sol.message or solver_name, info


# --------------------------------------------------------------------------- #
# Merge + diff
# --------------------------------------------------------------------------- #

def _merge_and_diff(augmented: models.Problem,
                    suffix_starts: Dict[str, int],
                    baseline: Optional[models.Solution],
                    now: int,
                    completed: Dict[str, Tuple[int, int, List[str]]],
                    inprog: Dict[str, Tuple[int, List[str], int]],
                    free_ids: List[str],
                    new_tasks: List[models.Task]) -> Tuple[Dict[str, int], List[models.ScheduleChange]]:
    tmap = augmented.task_map()
    b_start, b_end, b_res = _baseline_maps(baseline)
    new_ids = {t.id for t in new_tasks}

    merged: Dict[str, int] = {}
    changes: List[models.ScheduleChange] = []

    for tid, (s, e, res) in completed.items():
        merged[tid] = s
        changes.append(models.ScheduleChange(
            task=tid, change_type="completed",
            old_start=b_start.get(tid), old_end=b_end.get(tid),
            new_start=s, new_end=e,
            delta=_delta(b_start.get(tid), s), resources=res,
            note="已完成，保持不动"))

    for tid, (s, res, remaining) in inprog.items():
        e = now + remaining
        merged[tid] = s
        changes.append(models.ScheduleChange(
            task=tid, change_type="in_progress",
            old_start=b_start.get(tid), old_end=b_end.get(tid),
            new_start=s, new_end=e,
            delta=_delta(b_start.get(tid), s), resources=res,
            note=f"在制：剩余 {remaining}，将于 {e} 完工"))

    # free + rush tasks as they came out of the suffix solver
    for t in augmented.tasks:
        tid = t.id
        if tid in completed or tid in inprog:
            continue
        res = sorted(t.resource_requirements)
        if tid not in suffix_starts:
            changes.append(models.ScheduleChange(
                task=tid, change_type="unscheduled",
                old_start=b_start.get(tid), old_end=b_end.get(tid),
                new_start=None, new_end=None, delta=0, resources=res,
                note="在当前时间窗内无法重新排入"))
            continue
        s = suffix_starts[tid]
        e = s + t.duration
        merged[tid] = s
        if tid in new_ids:
            ctype = "added"
            note = "新插入急单"
        elif tid not in b_start:
            ctype = "unchanged"
            note = "基线中未排，本次排入"
        elif s == b_start[tid]:
            ctype = "unchanged"
            note = ""
        elif s > b_start[tid]:
            ctype = "delayed"
            note = f"延后 {s - b_start[tid]}"
        else:
            ctype = "advanced"
            note = f"提前 {b_start[tid] - s}"
        changes.append(models.ScheduleChange(
            task=tid, change_type=ctype,
            old_start=b_start.get(tid), old_end=b_end.get(tid),
            new_start=s, new_end=e,
            delta=_delta(b_start.get(tid), s), resources=res, note=note))

    return merged, changes


def _delta(old: Optional[int], new: int) -> int:
    return 0 if old is None else new - old


def _summarise(augmented: models.Problem, merged: Dict[str, int],
               changes: List[models.ScheduleChange],
               baseline: Optional[models.Solution], now: int
               ) -> Tuple[Dict[str, Any], Dict[str, Any], str, List[str]]:
    tmap = augmented.task_map()
    comp = objectives.completion_times(augmented, merged)
    ms = objectives.makespan(augmented, merged)
    violations = objectives.hard_violations(augmented, merged)
    unscheduled = [c.task for c in changes if c.change_type == "unscheduled"]
    feasible = not violations and not unscheduled

    shifted = [c for c in changes if c.change_type in ("delayed", "advanced")]
    abs_shifts = [abs(c.delta) for c in shifted]
    delays = [c.delta for c in changes if c.change_type == "delayed"]
    b_ms = baseline.makespan if baseline is not None else None

    n_completed = sum(c.change_type == "completed" for c in changes)
    n_inprog = sum(c.change_type == "in_progress" for c in changes)
    n_unchanged = sum(c.change_type == "unchanged" for c in changes)
    n_added = sum(c.change_type == "added" for c in changes)

    summary = {
        "now": now,
        "n_total": len(changes),
        "n_completed": n_completed,
        "n_in_progress": n_inprog,
        "n_unchanged": n_unchanged,
        "n_changed": len(shifted),
        "n_added": n_added,
        "n_unscheduled": len(unscheduled),
        "fraction_changed": round(len(shifted) / max(1, len(changes)), 3),
        "total_abs_shift": sum(abs_shifts),
        "max_abs_shift": max(abs_shifts, default=0),
        "total_delay": sum(delays),
        "makespan": ms,
        "baseline_makespan": b_ms,
        "makespan_delta": (ms - b_ms) if b_ms is not None else None,
        "unscheduled_tasks": unscheduled,
    }
    metrics = {
        "makespan": ms,
        "total_completion": sum(comp.values()),
        "total_tardiness": round(objectives.total_tardiness(augmented, merged), 3),
        "soft_penalty": round(objectives.soft_penalty(augmented, merged), 3),
        "n_violations": len(violations),
    }
    status = "feasible" if feasible else "infeasible"
    message = ""
    if unscheduled:
        message = f"{len(unscheduled)} 个任务无法排入：{', '.join(unscheduled[:5])}"
    elif violations:
        message = "; ".join(violations[:3])
    return summary, metrics, status, violations


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def reschedule(problem: models.Problem,
               now: int,
               *,
               baseline: Optional[models.Solution] = None,
               progress: Optional[List[models.TaskProgress]] = None,
               downtimes: Optional[List[models.ResourceDowntime]] = None,
               new_tasks: Optional[List[Dict[str, Any]]] = None,
               solver: str = "stability",
               params: Optional[Dict[str, Any]] = None,
               baseline_solution_id: Optional[str] = None) -> models.RescheduleResult:
    """Re-schedule the not-started remainder from time ``now``.

    See module docstring.  Raises ``ValueError`` for contradictory floor state
    (unknown tasks, bad intervals, running work on a broken machine, cycles
    introduced by rush orders, ...).
    """
    progress = progress or []
    downtimes = downtimes or []
    params = dict(params or {})
    stability_weight = float(params.pop("stability_weight", DEFAULT_STABILITY_WEIGHT))

    if solver not in models.RESCHEDULE_SOLVERS:
        raise ValueError(
            f"不支持的重排求解器：{solver}（可选 {', '.join(models.RESCHEDULE_SOLVERS)}）")

    # rush orders are parsed as ordinary tasks
    rush: List[models.Task] = []
    for d in new_tasks or []:
        try:
            rush.append(models.Task.from_dict(dict(d)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"急单任务定义无效：{exc}") from exc

    _pmap, completed, inprog, free_ids, actual_end = _normalise_state(
        problem, baseline, now, progress, downtimes, rush)

    # augmented problem = original + rush orders; used for cycle validation and
    # for evaluating the final merged schedule on real task durations
    augmented = _clone(problem)
    horizon = _suffix_horizon(problem, now, downtimes, rush)
    augmented.horizon = horizon

    # frozen tasks occupy their *actual* span (a task may overrun): completed
    # work fills [actual_start, actual_end), in-progress work fills
    # [actual_start, now + remaining).  Align durations so capacity checks and
    # metrics see reality rather than the planned duration.
    atmap = augmented.task_map()
    for tid, (s, e, _res) in completed.items():
        atmap[tid].duration = max(1, e - s)
    for tid, (s, _res, remaining) in inprog.items():
        atmap[tid].duration = max(1, now - s + remaining)

    augmented.tasks.extend(rush)
    errors = models.validate_problem(augmented)
    if errors:
        raise ValueError("加入急单后的问题校验失败：" + "; ".join(errors[:5]))

    b_start, _, _ = _baseline_maps(baseline)
    suffix = _suffix_problem(
        augmented, now, free_ids, completed, inprog, actual_end,
        downtimes, rush, b_start, stability_weight, horizon)

    suffix_starts, elapsed, sol_message, info = _solve_suffix(
        suffix, solver, params, free_ids, inprog, b_start, now)

    merged, changes = _merge_and_diff(
        augmented, suffix_starts, baseline, now, completed, inprog,
        free_ids, rush)
    summary, metrics, status, violations = _summarise(
        augmented, merged, changes, baseline, now)

    assignments = schedule_builder.schedule_to_assignments(augmented, merged)

    message = sol_message
    if status == "infeasible":
        if summary["unscheduled_tasks"]:
            tail = (f"{len(summary['unscheduled_tasks'])} 个任务排不进去："
                    f"{', '.join(summary['unscheduled_tasks'][:5])}")
        else:
            tail = f"{len(violations)} 项硬约束冲突"
        message = f"{message}；{tail}" if message else tail
    if info:
        metrics["suffix"] = info

    return models.RescheduleResult(
        id=models.new_id("rs"),
        problem_id=problem.id,
        baseline_solution_id=baseline_solution_id or (baseline.id if baseline else None),
        now=now,
        solver=solver,
        status=status,
        assignments=assignments,
        changes=sorted(changes, key=lambda c: (c.new_start if c.new_start is not None
                                               else 10 ** 9, c.task)),
        events={
            "now": now,
            "progress": [p.to_dict() for p in progress],
            "downtimes": [d.to_dict() for d in downtimes],
            "new_tasks": [t.to_dict() for t in rush],
        },
        summary=summary,
        metrics=metrics,
        params={**params, "stability_weight": stability_weight},
        solve_time=round(elapsed, 4),
        message=message)
