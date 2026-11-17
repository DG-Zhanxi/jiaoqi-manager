import argparse
import math
import random
import time
from collections import defaultdict, deque

import numpy as np

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    plt = None


# ================= 1. Data definition from the manuscript =================
# The task IDs in the manuscript are 1-based. The code keeps the original
# 0-based indices and does not change the given processing parameters.
resources = ["AGV", "R3", "R4", "R7", "R1", "R2", "R5", "R6"]

task_resource_map = {
    0: "AGV", 1: "AGV", 2: "R3", 3: "R4", 4: "R7", 5: "R4", 6: "R1", 7: "R2",
    8: "R3", 9: "R4", 10: "R5", 11: "R6", 12: "R4", 13: "R7", 14: "R4", 15: "R3",
    16: "R1", 17: "R2", 18: "R3", 19: "R4", 20: "R5", 21: "R6", 22: "AGV", 23: "AGV",
    24: "AGV", 25: "R3", 26: "R4", 27: "R7", 28: "R1", 29: "R2", 30: "R3", 31: "R4",
    32: "R5", 33: "R6", 34: "R3", 35: "R4", 36: "R1", 37: "R2", 38: "R3", 39: "R4",
    40: "R5", 41: "R6", 42: "AGV",
}

mode0_times = [
    38.8, 51.2, 193.0, 203.0, 38.0, 223.0, 115.0, 115.0, 36.0, 31.0,
    246.0, 244.0, 236.0, 79.0, 152.0, 146.0, 163.0, 163.0, 42.0, 42.0,
    403.0, 403.0, 50.8, 38.8, 46.4, 301.0, 213.0, 38.0, 95.0, 95.0,
    86.0, 86.0, 270.0, 270.0, 79.0, 71.0, 316.0, 316.0, 207.0, 207.0,
    592.0, 586.0, 53.2,
]

# Optional collaborative accelerated mode from Table 2. Missing entries mean
# that the task has only Mode 0 in the manuscript.
mode1_times = {
    2: 135.1,
    3: 142.1,
    5: 156.1,
    8: 25.2,
    9: 21.7,
    12: 165.2,
    14: 106.4,
    15: 102.2,
    18: 29.4,
    19: 29.4,
    24: 210.7,
    25: 149.1,
    30: 60.2,
    31: 60.2,
    34: 55.3,
    35: 49.7,
    38: 144.9,
    39: 144.9,
}

# The manuscript states that Mode 1 incurs 10 units of man-hour cost and that
# the total man-hour budget is Cr = 80.
MODE1_COST = 10
MAN_HOUR_BUDGET = 80

predecessors = {
    0: [], 1: [0], 2: [0], 3: [2], 4: [2], 5: [4], 6: [4], 7: [4],
    8: [4], 9: [4], 10: [4], 11: [9], 12: [], 13: [], 14: [], 15: [],
    16: [], 17: [14], 18: [13], 19: [], 20: [], 21: [19, 20], 22: [],
    23: [], 24: [23], 25: [23], 26: [], 27: [], 28: [], 29: [], 30: [],
    31: [], 32: [], 33: [31, 32], 34: [33], 35: [], 36: [], 37: [],
    38: [], 39: [], 40: [], 41: [39], 42: [],
}


class SchedulerParams:
    def __init__(
        self,
        mode0_times,
        mode1_times,
        task_resource,
        predecessors,
        man_hour_budget=MAN_HOUR_BUDGET,
        mode1_cost=MODE1_COST,
    ):
        self.n_jobs = len(mode0_times)
        self.mode0_times = list(mode0_times)
        self.mode1_times = dict(mode1_times)
        self.task_resource = task_resource
        self.predecessors = predecessors
        self.man_hour_budget = man_hour_budget
        self.mode1_cost = mode1_cost
        self.mode1_jobs = sorted(mode1_times)

        self.successors = defaultdict(list)
        for job, preds in predecessors.items():
            for pred in preds:
                self.successors[pred].append(job)

        avg_time = np.mean(mode0_times)
        self.selection_weights = []
        for i in range(self.n_jobs):
            w = 1.0
            if task_resource[i] == "R4":
                w += 2.0
            if mode0_times[i] > avg_time:
                w += 1.0
            if i in self.mode1_times:
                w += 1.0
            self.selection_weights.append(w)

    def valid_modes(self, job):
        return [0, 1] if job in self.mode1_times else [0]

    def processing_time(self, job, mode):
        if mode == 1:
            return self.mode1_times[job]
        return self.mode0_times[job]

    def mode_cost(self, job, mode):
        return self.mode1_cost if mode == 1 else 0


def mode_budget_cost(modes, params):
    return sum(params.mode_cost(job, modes[job]) for job in range(params.n_jobs))


def repair_modes(modes, params):
    repaired = list(modes)
    for job in range(params.n_jobs):
        if job not in params.mode1_times:
            repaired[job] = 0

    while mode_budget_cost(repaired, params) > params.man_hour_budget:
        active = [job for job in params.mode1_jobs if repaired[job] == 1]
        if not active:
            break
        # Remove the least useful accelerated choice first. This keeps the
        # repair deterministic enough while preserving a broad search space.
        job_to_reset = min(
            active,
            key=lambda job: params.mode0_times[job] - params.mode1_times[job],
        )
        repaired[job_to_reset] = 0
    return repaired


def random_modes(params):
    modes = [0] * params.n_jobs
    candidates = params.mode1_jobs[:]
    random.shuffle(candidates)
    for job in candidates:
        if mode_budget_cost(modes, params) + params.mode1_cost <= params.man_hour_budget:
            if random.random() < 0.5:
                modes[job] = 1
    return modes


def greedy_budget_modes(schedule, params):
    modes = [0] * params.n_jobs
    max_upgrades = int(params.man_hour_budget // params.mode1_cost)
    order = {job: idx for idx, job in enumerate(schedule)}
    candidates = [
        job for job in params.mode1_jobs
        if params.mode0_times[job] > params.mode1_times[job]
    ]
    candidates.sort(
        key=lambda job: (
            params.selection_weights[job] * (params.mode0_times[job] - params.mode1_times[job]),
            -order.get(job, params.n_jobs),
        ),
        reverse=True,
    )
    for job in candidates[:max_upgrades]:
        modes[job] = 1
    return modes


def make_solution(params):
    return {
        "seq": random.sample(range(params.n_jobs), params.n_jobs),
        "modes": random_modes(params),
    }


def clone_solution(solution):
    return {"seq": solution["seq"][:], "modes": solution["modes"][:]}


# ================= 2. Multi-mode decoder =================
def decode_solution(solution, params):
    """Serial schedule generation scheme for a priority vector plus mode vector."""
    priority_perm = solution["seq"]
    modes = repair_modes(solution["modes"], params)
    priorities = {job: idx for idx, job in enumerate(priority_perm)}
    indegree = {i: len(params.predecessors[i]) for i in range(params.n_jobs)}
    ready = [i for i in range(params.n_jobs) if indegree[i] == 0]

    resource_end = defaultdict(float)
    completion_times = [0.0] * params.n_jobs
    start_times = [0.0] * params.n_jobs
    scheduled_order = []

    while ready:
        best_job = min(ready, key=lambda k: priorities[k])
        ready.remove(best_job)

        pred_max = 0.0
        if params.predecessors[best_job]:
            pred_max = max(completion_times[p] for p in params.predecessors[best_job])

        mode = modes[best_job]
        res = params.task_resource[best_job]
        start = max(resource_end[res], pred_max)
        end = start + params.processing_time(best_job, mode)

        start_times[best_job] = start
        completion_times[best_job] = end
        resource_end[res] = end
        scheduled_order.append(best_job)

        for succ in params.successors[best_job]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(succ)

    if len(scheduled_order) < params.n_jobs:
        return {
            "makespan": float("inf"),
            "cost": float("inf"),
            "feasible": False,
            "start_times": start_times,
            "completion_times": completion_times,
            "schedule": scheduled_order,
            "modes": modes,
        }

    total_cost = mode_budget_cost(modes, params)
    feasible = total_cost <= params.man_hour_budget
    return {
        "makespan": max(completion_times),
        "cost": total_cost,
        "feasible": feasible,
        "start_times": start_times,
        "completion_times": completion_times,
        "schedule": scheduled_order,
        "modes": modes,
    }


def objective(solution, params):
    decoded = decode_solution(solution, params)
    if decoded["feasible"]:
        return decoded["makespan"]
    excess = decoded["cost"] - params.man_hour_budget
    return decoded["makespan"] + 1_000_000.0 * max(0.0, excess)


# ================= 3. Comparative algorithms =================
GA_CROSSOVER_RATE = 0.8
GA_MUTATION_RATE = 0.2
DPSO_INERTIA_WEIGHT = 0.4
DPSO_COGNITIVE_FACTOR = 1.5
DPSO_SOCIAL_FACTOR = 1.5
EDA_TRUNCATION_RATIO = 0.1
EDA_LEARNING_RATE = 0.3

# Fill this with the CPLEX/Gurobi proven optimal objective for the same
# instance. Alternatively, run: python 对比算法.md --exact-cmax <value>
EXACT_REFERENCE_CMAX = None


def order_crossover(p1, p2):
    size = len(p1)
    a, b = sorted(random.sample(range(size), 2))
    child = [-1] * size
    child[a:b + 1] = p1[a:b + 1]
    current_genes = set(child[a:b + 1])
    p2_idx = 0
    for i in range(size):
        if child[i] == -1:
            while p2[p2_idx] in current_genes:
                p2_idx += 1
            child[i] = p2[p2_idx]
            current_genes.add(p2[p2_idx])
    return child


def mode_uniform_crossover(m1, m2, params):
    child = [m1[i] if random.random() < 0.5 else m2[i] for i in range(params.n_jobs)]
    return repair_modes(child, params)


def mutate_solution(solution, params, seq_rate=GA_MUTATION_RATE, mode_rate=GA_MUTATION_RATE):
    child = clone_solution(solution)
    n = params.n_jobs
    if random.random() < seq_rate:
        i, j = random.sample(range(n), 2)
        child["seq"][i], child["seq"][j] = child["seq"][j], child["seq"][i]
    for job in params.mode1_jobs:
        if random.random() < mode_rate:
            child["modes"][job] = 1 - child["modes"][job]
    child["modes"] = repair_modes(child["modes"], params)
    return child


def apply_guided_swaps(seq, target_seq, max_swaps):
    """Move a permutation toward a target permutation by a bounded swap sequence."""
    if max_swaps <= 0:
        return
    for _ in range(max_swaps):
        mismatches = [idx for idx, job in enumerate(seq) if job != target_seq[idx]]
        if not mismatches:
            return
        target_pos = random.choice(mismatches)
        target_job = target_seq[target_pos]
        current_pos = seq.index(target_job)
        if current_pos != target_pos:
            seq[current_pos], seq[target_pos] = seq[target_pos], seq[current_pos]


def run_ga(params, max_gen=100, pop_size=20):
    population = [make_solution(params) for _ in range(pop_size)]
    history = []
    best_solution = None
    best_fit = float("inf")

    for _ in range(max_gen):
        fitness = [objective(ind, params) for ind in population]
        gen_best_idx = int(np.argmin(fitness))
        if fitness[gen_best_idx] < best_fit:
            best_fit = fitness[gen_best_idx]
            best_solution = clone_solution(population[gen_best_idx])
        history.append(best_fit)

        new_pop = []
        sorted_idx = np.argsort(fitness)
        new_pop.extend([clone_solution(population[i]) for i in sorted_idx[:2]])

        while len(new_pop) < pop_size:
            p1 = population[min(random.sample(range(pop_size), 3), key=lambda k: fitness[k])]
            p2 = population[min(random.sample(range(pop_size), 3), key=lambda k: fitness[k])]

            if random.random() < GA_CROSSOVER_RATE:
                seq = order_crossover(p1["seq"], p2["seq"])
                modes = mode_uniform_crossover(p1["modes"], p2["modes"], params)
                child = {"seq": seq, "modes": modes}
            else:
                child = clone_solution(p1)

            child = mutate_solution(child, params, seq_rate=GA_MUTATION_RATE, mode_rate=GA_MUTATION_RATE)
            new_pop.append(child)
        population = new_pop

    return history, best_fit, best_solution


def run_dpso(params, max_iter=100, pop_size=20):
    particles = [make_solution(params) for _ in range(pop_size)]
    pbest = [clone_solution(x) for x in particles]
    pbest_fit = [objective(x, params) for x in particles]

    gbest_idx = int(np.argmin(pbest_fit))
    gbest = clone_solution(pbest[gbest_idx])
    gbest_fit = pbest_fit[gbest_idx]
    history = []

    w = DPSO_INERTIA_WEIGHT
    c1 = DPSO_COGNITIVE_FACTOR
    c2 = DPSO_SOCIAL_FACTOR
    cognitive_swaps = max(1, int(math.ceil(c1 * params.n_jobs)))
    social_swaps = max(1, int(math.ceil(c2 * params.n_jobs)))
    inertia_swaps = max(1, int(math.ceil(w * params.n_jobs)))

    for _ in range(max_iter):
        for i in range(pop_size):
            seq = particles[i]["seq"]
            modes = particles[i]["modes"]

            apply_guided_swaps(seq, pbest[i]["seq"], cognitive_swaps)
            apply_guided_swaps(seq, gbest["seq"], social_swaps)

            for _ in range(inertia_swaps):
                idx1, idx2 = random.sample(range(params.n_jobs), 2)
                seq[idx1], seq[idx2] = seq[idx2], seq[idx1]

            for job in params.mode1_jobs:
                r = random.random()
                if r < 0.15:
                    modes[job] = pbest[i]["modes"][job]
                elif r < 0.30:
                    modes[job] = gbest["modes"][job]
                elif r < 0.35:
                    modes[job] = 1 - modes[job]
            particles[i]["modes"] = repair_modes(modes, params)

            fit = objective(particles[i], params)
            if fit < pbest_fit[i]:
                pbest[i] = clone_solution(particles[i])
                pbest_fit[i] = fit
                if fit < gbest_fit:
                    gbest = clone_solution(particles[i])
                    gbest_fit = fit
        history.append(gbest_fit)
    return history, gbest_fit, gbest


def run_eda(params, max_gen=100, pop_size=20):
    n = params.n_jobs
    prob_matrix = np.ones((n, n)) / n
    mode_prob = np.zeros(n)
    for job in params.mode1_jobs:
        mode_prob[job] = 0.5
    alpha = EDA_LEARNING_RATE
    history = []
    best_solution = None
    best_fit = float("inf")

    for _ in range(max_gen):
        pop = []
        for _ in range(pop_size):
            perm = []
            available = list(range(n))
            for pos in range(n):
                probs = prob_matrix[pos][available]
                probs_sum = probs.sum()
                probs = np.ones(len(available)) / len(available) if probs_sum == 0 else probs / probs_sum
                chosen = int(np.random.choice(available, p=probs))
                perm.append(chosen)
                available.remove(chosen)

            modes = [0] * n
            for job in params.mode1_jobs:
                modes[job] = 1 if random.random() < mode_prob[job] else 0
            modes = repair_modes(modes, params)
            pop.append({"seq": perm, "modes": modes})

        fitness = [objective(p, params) for p in pop]
        gen_best_idx = int(np.argmin(fitness))
        if fitness[gen_best_idx] < best_fit:
            best_fit = fitness[gen_best_idx]
            best_solution = clone_solution(pop[gen_best_idx])
        history.append(best_fit)

        elite_count = max(1, int(pop_size * EDA_TRUNCATION_RATIO))
        elite_idx = np.argsort(fitness)[:elite_count]
        new_prob = np.zeros((n, n))
        new_mode_prob = np.zeros(n)
        for idx in elite_idx:
            for pos, job in enumerate(pop[idx]["seq"]):
                new_prob[pos, job] += 1
            for job in params.mode1_jobs:
                new_mode_prob[job] += pop[idx]["modes"][job]
        new_prob /= elite_count
        new_mode_prob /= elite_count
        prob_matrix = (1 - alpha) * prob_matrix + alpha * new_prob
        mode_prob = (1 - alpha) * mode_prob + alpha * new_mode_prob

    return history, best_fit, best_solution


# ================= 4. Proposed adaptive hybrid SA =================
def grasp_initial(params):
    """Original topology-based GRASP initialization, with a budget-feasible mode vector added."""
    indegree = {i: len(params.predecessors[i]) for i in range(params.n_jobs)}
    ready = [i for i in range(params.n_jobs) if indegree[i] == 0]
    schedule = []
    while ready:
        job = random.choice(ready)
        schedule.append(job)
        ready.remove(job)
        for succ in params.successors[job]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(succ)
    return {"seq": schedule, "modes": greedy_budget_modes(schedule, params)}


def get_adaptive_sequence_neighbor(schedule, params, intensity="low"):
    """Original DNO sequence move: weighted extraction and feasible-window insertion."""
    new_schedule = list(schedule)
    n_moves = max(2, int(params.n_jobs * 0.15)) if intensity == "high" else 1
    for _ in range(n_moves):
        n = len(new_schedule)
        pos = {job: i for i, job in enumerate(new_schedule)}
        job_curr = random.choices(new_schedule, weights=[params.selection_weights[j] for j in new_schedule], k=1)[0]
        curr_idx = pos[job_curr]
        preds = params.predecessors[job_curr]
        valid_min = max([pos[p] for p in preds]) + 1 if preds else 0
        succs = params.successors[job_curr]
        valid_max = min([pos[s] for s in succs]) - 1 if succs else n - 1

        if valid_min < valid_max:
            possible_indices = [i for i in range(valid_min, valid_max + 1) if i != curr_idx]
            if possible_indices:
                new_idx = random.choice(possible_indices)
                new_schedule.pop(curr_idx)
                new_schedule.insert(new_idx, job_curr)
    return new_schedule


def mode_mutation_probability(iteration, max_iter):
    progress = iteration / max_iter
    if progress < 0.3:
        return 0.60 - 0.30 * (progress / 0.3)
    return 0.05


def perturb_modes(modes, params, intensity="low"):
    new_modes = list(modes)
    n_flips = 3 if intensity == "high" else 1
    for _ in range(n_flips):
        active = [job for job in params.mode1_jobs if new_modes[job] == 1]
        inactive = [job for job in params.mode1_jobs if new_modes[job] == 0]
        if inactive and mode_budget_cost(new_modes, params) + params.mode1_cost <= params.man_hour_budget:
            job = max(
                inactive,
                key=lambda item: params.selection_weights[item] * (params.mode0_times[item] - params.mode1_times[item]),
            )
            new_modes[job] = 1
        elif active and inactive:
            remove_job = min(
                active,
                key=lambda item: params.selection_weights[item] * (params.mode0_times[item] - params.mode1_times[item]),
            )
            add_job = max(
                inactive,
                key=lambda item: params.selection_weights[item] * (params.mode0_times[item] - params.mode1_times[item]),
            )
            if add_job != remove_job:
                new_modes[remove_job] = 0
                new_modes[add_job] = 1
        elif active:
            job = random.choice(active)
            new_modes[job] = 0
    return repair_modes(new_modes, params)


def get_adaptive_neighbor(solution, params, iteration, max_iter, intensity="low"):
    new_solution = clone_solution(solution)

    if random.random() < mode_mutation_probability(iteration, max_iter):
        new_solution["modes"] = perturb_modes(new_solution["modes"], params, intensity)
    else:
        new_solution["seq"] = get_adaptive_sequence_neighbor(new_solution["seq"], params, intensity)

    if intensity == "high":
        new_solution["seq"] = get_adaptive_sequence_neighbor(new_solution["seq"], params, intensity)
    return new_solution


def run_proposed(params, max_iter=2000):
    current = grasp_initial(params)
    current_cost = objective(current, params)
    best_cost = current_cost
    best_solution = clone_solution(current)
    temperature = current_cost * 0.5
    cooling_rate = 0.98
    check_interval = 50
    high_acceptance = 0.4
    low_acceptance = 0.1
    accepted_in_window = 0
    history = []
    history_win = deque(maxlen=10)

    for i in range(max_iter):
        intensity = "high" if i < 0.3 * max_iter else "low"
        new_solution = get_adaptive_neighbor(current, params, i, max_iter, intensity)
        new_cost = objective(new_solution, params)

        delta = new_cost - current_cost
        if delta < 0:
            accept_move = True
        else:
            accept_prob = math.exp(-delta / temperature) if temperature > 1e-12 else 0.0
            accept_move = random.random() < accept_prob
        if accept_move:
            current = new_solution
            current_cost = new_cost
            accepted_in_window += 1
            if new_cost < best_cost:
                best_cost = new_cost
                best_solution = clone_solution(new_solution)

        history.append(best_cost)
        history_win.append(best_cost)

        if (i + 1) % check_interval == 0:
            acceptance_ratio = accepted_in_window / check_interval
            if acceptance_ratio > high_acceptance:
                cooling_rate = 0.90
            elif acceptance_ratio < low_acceptance:
                cooling_rate = 0.99
            else:
                cooling_rate = 0.98
            accepted_in_window = 0
        temperature *= cooling_rate

        if i > 200 and len(history_win) == 10 and np.std(history_win) < 1e-4:
            current = get_adaptive_neighbor(best_solution, params, i, max_iter, "high")
            current_cost = objective(current, params)
            temperature = max(1.0, temperature + 0.05 * best_cost)
            history_win.clear()

    return history, best_cost, best_solution


# ================= 5. Main program and visualization =================
def normalize_len(data, target_len=100):
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, target_len)
    return np.interp(x_new, x_old, data)


def print_solution_summary(name, best_cost, elapsed, solution, params):
    decoded = decode_solution(solution, params)
    selected_mode1 = [job + 1 for job, mode in enumerate(decoded["modes"]) if mode == 1]
    print(
        f"{name:<15} | {best_cost:<10.1f} | {decoded['cost']:<6.1f} | "
        f"{elapsed:<10.2f} | {selected_mode1}"
    )


def compute_optimality_gap(cmax, exact_cmax):
    if exact_cmax is None:
        return None
    if exact_cmax <= 0:
        raise ValueError("The exact reference Cmax must be positive.")
    return (cmax - exact_cmax) / exact_cmax * 100.0


def format_gap(gap):
    if gap is None:
        return "N/A"
    return f"{gap:.2f}"


def run_independent_trials(name, runner, params, n_trials=30, seed_offset=0, **runner_kwargs):
    makespans = []
    costs = []
    elapsed_times = []
    best_solution = None
    best_history = None
    best_makespan = float("inf")

    print(f"Running {name} for {n_trials} independent trials...")
    for trial in range(n_trials):
        seed = 42 + seed_offset * 1000 + trial
        random.seed(seed)
        np.random.seed(seed)

        start = time.time()
        history, makespan, solution = runner(params, **runner_kwargs)
        elapsed = time.time() - start
        decoded = decode_solution(solution, params)

        makespans.append(makespan)
        costs.append(decoded["cost"])
        elapsed_times.append(elapsed)

        if makespan < best_makespan:
            best_makespan = makespan
            best_solution = clone_solution(solution)
            best_history = history[:]

    makespans_arr = np.array(makespans, dtype=float)
    costs_arr = np.array(costs, dtype=float)
    times_arr = np.array(elapsed_times, dtype=float)
    best_decoded = decode_solution(best_solution, params)

    return {
        "name": name,
        "best_makespan": float(np.min(makespans_arr)),
        "mean_makespan": float(np.mean(makespans_arr)),
        "std_makespan": float(np.std(makespans_arr, ddof=1)) if len(makespans_arr) > 1 else 0.0,
        "best_cost": float(best_decoded["cost"]),
        "mean_cost": float(np.mean(costs_arr)),
        "mean_time": float(np.mean(times_arr)),
        "best_solution": best_solution,
        "best_history": best_history,
        "all_makespans": makespans,
        "all_costs": costs,
        "all_times": elapsed_times,
    }


def print_trial_summary(summary, params):
    selected_mode1 = [
        job + 1
        for job, mode in enumerate(summary["best_solution"]["modes"])
        if mode == 1
    ]
    print(
        f"{summary['name']:<15} | {summary['best_makespan']:<13.1f} | "
        f"{summary['mean_makespan']:<13.2f} | {summary['std_makespan']:<10.2f} | "
        f"{format_gap(summary['best_gap_pct']):<12} | {format_gap(summary['mean_gap_pct']):<12} | "
        f"{summary['mean_cost']:<9.2f} | {summary['mean_time']:<13.4f} | "
        f"{selected_mode1}"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run 30 independent metaheuristic trials and report optimality gaps."
    )
    parser.add_argument(
        "--exact-cmax",
        type=float,
        default=EXACT_REFERENCE_CMAX,
        help="CPLEX/Gurobi proven optimal Cmax for this exact instance.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=30,
        help="Number of independent runs for each algorithm. Default is 30.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    params = SchedulerParams(mode0_times, mode1_times, task_resource_map, predecessors)
    n_trials = args.trials
    exact_cmax = args.exact_cmax

    print(f"Starting multi-mode comparative analysis with Cr = 80 and {n_trials} independent trials...")
    if exact_cmax is None:
        print(
            "Exact reference Cmax is not set. Gap columns will be N/A; "
            "set EXACT_REFERENCE_CMAX or pass --exact-cmax after solving the same instance with CPLEX/Gurobi."
        )
    else:
        print(f"Exact reference Cmax for gap calculation: {exact_cmax:.4f}")

    summaries = {}
    summaries["Proposed"] = run_independent_trials(
        "Proposed",
        run_proposed,
        params,
        n_trials=n_trials,
        seed_offset=1,
        max_iter=2000,
    )
    summaries["GA"] = run_independent_trials(
        "GA",
        run_ga,
        params,
        n_trials=n_trials,
        seed_offset=2,
        max_gen=100,
        pop_size=20,
    )
    summaries["DPSO"] = run_independent_trials(
        "DPSO",
        run_dpso,
        params,
        n_trials=n_trials,
        seed_offset=3,
        max_iter=100,
        pop_size=20,
    )
    summaries["EDA"] = run_independent_trials(
        "EDA",
        run_eda,
        params,
        n_trials=n_trials,
        seed_offset=4,
        max_gen=100,
        pop_size=20,
    )

    for summary in summaries.values():
        summary["best_gap_pct"] = compute_optimality_gap(summary["best_makespan"], exact_cmax)
        summary["mean_gap_pct"] = compute_optimality_gap(summary["mean_makespan"], exact_cmax)

    print(f"\n=== Final Results over {n_trials} Independent Runs ===")
    print(
        f"{'Algorithm':<15} | {'Best Cmax':<13} | {'Mean Cmax':<13} | "
        f"{'STD':<10} | {'Best Gap(%)':<12} | {'Mean Gap(%)':<12} | "
        f"{'Mean Cost':<9} | {'Mean Time(s)':<13} | Best Mode-1 tasks"
    )
    print("-" * 160)
    for algo in ["Proposed", "GA", "DPSO", "EDA"]:
        print_trial_summary(summaries[algo], params)

    if plt is not None:
        plt.rcParams.update({"font.size": 12})
        custom_colors = ["red", "blue", "orange", "green"]
        target_points = 1000

        plt.figure(figsize=(10, 6), dpi=300)
        for idx, algo in enumerate(["Proposed", "GA", "DPSO", "EDA"]):
            plt.plot(
                normalize_len(summaries[algo]["best_history"], target_points),
                label=algo,
                color=custom_colors[idx],
                linewidth=2.5 if algo == "Proposed" else 1.8,
                linestyle="-" if algo == "Proposed" else "--",
            )
        plt.xlabel("Iteration", fontsize=12)
        plt.ylabel("Makespan", fontsize=12)
        plt.legend()
        plt.grid(True, linestyle=":", alpha=0.6)
        plt.tight_layout()
        plt.show()

        plt.figure(figsize=(10, 6), dpi=300)
        algos = list(summaries.keys())
        t_vals = [summaries[algo]["mean_time"] for algo in algos]
        bars = plt.bar(algos, t_vals, color=custom_colors, alpha=0.8, width=0.6)
        plt.ylabel("Mean time (seconds)", fontsize=12)
        plt.grid(axis="y", linestyle="--", alpha=0.5)
        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width() / 2, yval, f"{yval:.2f}s", va="bottom", ha="center", fontsize=11)
        plt.tight_layout()
        plt.show()
    else:
        print("\nmatplotlib is not installed; skipped convergence and runtime plots.")
