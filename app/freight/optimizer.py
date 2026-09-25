"""The route optimizer: a real, small vehicle-routing solver.

Given a depot, N delivery stops and K trucks, plan who drives where so the
total distance is short and no truck is overloaded. It starts from a naive
plan -- stops dealt out in random order to whichever truck has the most room,
the way a new dispatcher might -- and improves it by ITERATED LOCAL SEARCH:

  * local search: move any stop to any position on any truck, and untangle
    each route (2-opt), until nothing shortens the plan any more;
  * then each iteration shakes the best plan up (a few stops moved at
    random), searches again from there, and keeps the result only if it is
    shorter. Shaking is how the search escapes a plan that is merely "not
    improvable by one move".

Every iteration reports the plan it just found and the best so far: the live
chart on the job page (the best line only ever goes down).

The arithmetic is light -- every move is priced in O(1) from the distance
matrix. The RELEASE decides how long an iteration takes (optimizer_step_s),
which is how 2.0.0's "the optimizer got twice as slow" regression is planted.
Deterministic for a given (region, stops, trucks, seed), so a test can assert
on the outcome.
"""
import csv
import io
import math
import random

from freight import catalog
from freight.regions import REGIONS

ROAD_FACTOR = 1.25
AVG_KMH = 68.0
MINUTES_PER_STOP = 12
CO2_KG_PER_KM = 0.95
STOPS_RANGE = (5, 45)      # every region has at least 48 candidate cities
TRUCKS_RANGE = (1, 8)
EPS = 1e-6
TRIES_PER_ITERATION = 4     # shake+search rounds between two progress reports
COLORS = ["#e6194b", "#2f9e44", "#4263eb", "#f08c00", "#9c36b5",
          "#0c8599", "#d6336c", "#795548"]


def iterations_for(stops):
    return min(90, 20 + int(stops))


def build_problem(region, stops, trucks, seed):
    rng = random.Random(f"{region}|{stops}|{trucks}|{seed}")
    depot = catalog.depot(region)
    candidates = catalog.region_stops(region)
    chosen = rng.sample(candidates, min(int(stops), len(candidates)))
    demand = [0] + [rng.randint(1, 6) for _ in chosen]
    # Room for the greedy start to be feasible by construction: the emptiest
    # truck never holds more than the average, plus one more stop.
    capacity = math.ceil(sum(demand) / trucks) + max(demand)
    points = [depot] + chosen
    n = len(points)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = catalog.haversine_km(points[i], points[j]) * ROAD_FACTOR
            dist[i][j] = dist[j][i] = d
    return rng, points, demand, capacity, dist


def route_km(route, dist):
    if not route:
        return 0.0
    total = dist[0][route[0]] + dist[route[-1]][0]
    for a, b in zip(route, route[1:]):
        total += dist[a][b]
    return total


def plan_km(routes, dist):
    return sum(route_km(r, dist) for r in routes)


def naive_plan(rng, n_stops, demand, trucks):
    order = list(range(1, n_stops + 1))
    rng.shuffle(order)
    routes = [[] for _ in range(trucks)]
    load = [0] * trucks
    for stop in order:
        t = min(range(trucks), key=lambda i: load[i])
        routes[t].append(stop)
        load[t] += demand[stop]
    return routes


def _relocate_pass(routes, load, demand, capacity, dist):
    """First-improvement: any stop to any position on any truck."""
    improved = False
    for a in range(len(routes)):
        i = 0
        while i < len(routes[a]):
            ra = routes[a]
            stop = ra[i]
            prev = ra[i - 1] if i > 0 else 0
            nxt = ra[i + 1] if i < len(ra) - 1 else 0
            gain = dist[prev][stop] + dist[stop][nxt] - dist[prev][nxt]
            without = ra[:i] + ra[i + 1:]
            moved = False
            for b in range(len(routes)):
                if b != a and load[b] + demand[stop] > capacity:
                    continue
                base = without if b == a else routes[b]
                for j in range(len(base) + 1):
                    p = base[j - 1] if j > 0 else 0
                    q = base[j] if j < len(base) else 0
                    if dist[p][stop] + dist[stop][q] - dist[p][q] - gain < -EPS:
                        routes[a] = without
                        routes[b] = base[:j] + [stop] + base[j:]
                        if b != a:
                            load[a] -= demand[stop]
                            load[b] += demand[stop]
                        moved = improved = True
                        break
                if moved:
                    break
            if not moved:
                i += 1
    return improved


def _two_opt_pass(route, dist):
    """First-improvement 2-opt on one route, in place; O(1) per move."""
    improved = False
    n = len(route)
    again = True
    while again:
        again = False
        for i in range(n - 1):
            p = route[i - 1] if i > 0 else 0
            for j in range(i + 1, n):
                q = route[j + 1] if j < n - 1 else 0
                delta = (dist[p][route[j]] + dist[route[i]][q]
                         - dist[p][route[i]] - dist[route[j]][q])
                if delta < -EPS:
                    route[i:j + 1] = route[i:j + 1][::-1]
                    again = improved = True
                    break
            if again:
                break
    return improved


def local_search(routes, demand, capacity, dist):
    """Relocate + 2-opt until neither shortens the plan."""
    routes = [list(r) for r in routes]
    load = [sum(demand[s] for s in r) for r in routes]
    while True:
        moved = _relocate_pass(routes, load, demand, capacity, dist)
        untangled = False
        for route in routes:
            untangled = _two_opt_pass(route, dist) or untangled
        if not (moved or untangled):
            return routes


def shake(routes, rng, demand, capacity, k):
    """Move k random stops to random feasible places."""
    routes = [list(r) for r in routes]
    load = [sum(demand[s] for s in r) for r in routes]
    stops = [s for r in routes for s in r]
    for stop in rng.sample(stops, min(k, len(stops))):
        for t, r in enumerate(routes):
            if stop in r:
                r.remove(stop)
                load[t] -= demand[stop]
                break
        fits = [t for t in range(len(routes)) if load[t] + demand[stop] <= capacity]
        t = rng.choice(fits)
        routes[t].insert(rng.randint(0, len(routes[t])), stop)
        load[t] += demand[stop]
    return routes


def run(job, params):
    region = params["region"]
    stops = int(params["stops"])
    trucks = int(params["trucks"])
    seed = int(params.get("seed", 42))
    step_s = float(params.get("step_s", 0.5))

    job.update(0.01, "Building the distance matrix")
    rng, points, demand, capacity, dist = build_problem(region, stops, trucks, seed)
    n_stops = len(points) - 1
    job.say(f"{n_stops} stops around {points[0].name}, {trucks} trucks, "
            f"capacity {capacity} pallets each")
    job.sleep(step_s)

    naive = naive_plan(rng, n_stops, demand, trucks)
    initial = plan_km(naive, dist)
    job.say(f"naive plan (stops dealt out at random): {initial:,.0f} km")
    job.point(0, round(initial, 1), round(initial, 1))

    total_iter = iterations_for(stops)
    strength = max(2, n_stops // 5)
    best_routes, best = None, initial
    for it in range(total_iter):
        for _ in range(1 if best_routes is None else TRIES_PER_ITERATION):
            start = naive if best_routes is None else shake(best_routes, rng, demand,
                                                            capacity, strength)
            cand = local_search(start, demand, capacity, dist)
            current = plan_km(cand, dist)
            if best_routes is None or current < best - EPS:
                best_routes, best = cand, current
        job.point(it + 1, round(current, 1), round(best, 1))
        job.update((it + 1) / (total_iter + 1),
                   f"Iteration {it + 1} of {total_iter} -- best {best:,.0f} km")
        if (it + 1) % 5 == 0 or it == 0:
            job.say(f"iteration {it + 1}: best {best:,.0f} km "
                    f"({100 * (initial - best) / initial:.1f}% shorter than naive)")
        job.sleep(step_s)

    job.update(total_iter / (total_iter + 1), "Checking the final plan")
    job.say(f"final plan: {best:,.0f} km, {100 * (initial - best) / initial:.1f}% "
            f"shorter than the naive plan")
    job.sleep(step_s)
    return _result(region, points, demand, capacity, trucks, best_routes,
                   initial, best, total_iter, dist)


def _result(region, points, demand, capacity, trucks, routes, initial, best,
            iterations, dist):
    out_routes = []
    for t, route in enumerate(routes):
        if not route:
            continue
        km = route_km(route, dist)
        path = [points[0]] + [points[s] for s in route] + [points[0]]
        out_routes.append({
            "truck": t + 1,
            "color": COLORS[t % len(COLORS)],
            "stops": [points[s].name for s in route],
            "load": sum(demand[s] for s in route),
            "km": round(km, 1),
            "hours": round(km / AVG_KMH + len(route) * MINUTES_PER_STOP / 60, 1),
            "path": [[p.lat, p.lon] for p in path],
        })
    return {
        "region": region,
        "region_label": REGIONS[region]["label"],
        "depot": points[0].name,
        "depot_point": [points[0].lat, points[0].lon],
        "stops": len(points) - 1,
        "stop_points": [[p.name, p.lat, p.lon, demand[i + 1]]
                        for i, p in enumerate(points[1:])],
        "trucks": trucks,
        "trucks_used": len(out_routes),
        "capacity": capacity,
        "initial_km": round(initial, 1),
        "best_km": round(best, 1),
        "saving_pct": round(100 * (initial - best) / initial, 1) if initial else 0.0,
        "co2_kg": int(round(best * CO2_KG_PER_KM)),
        "iterations": iterations,
        "routes": out_routes,
    }


def plan_csv(result):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["truck", "sequence", "stop", "route_km", "route_hours",
                     "route_load_pallets"])
    for route in result["routes"]:
        for seq, stop in enumerate(route["stops"], start=1):
            writer.writerow([route["truck"], seq, stop, route["km"],
                             route["hours"], route["load"]])
    return buf.getvalue()
