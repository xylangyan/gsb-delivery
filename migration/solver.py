"""Exact integer feasibility solver for key-collision checks.

The solver decides whether a set of constraints over integer variables
with (possibly huge) interval domains is satisfiable, and returns a
concrete assignment when it is.  It never enumerates the domains:
propagation is interval-based and exact for the supported constraints,
and completeness comes from bisection branching, which always
terminates because the domains are finite sets of integers.

Supported constraints:

* linear equalities           ``sum(c_i * x_i) == k``
* lookup relations            ``(x, y) in pairs`` (finite table)
* sign-extension relations    ``y == sign_extend_bits(x)``

All propagation is sound (it never removes a feasible value); hull
approximations are only used together with branching, so the procedure
as a whole is exact.
"""

from __future__ import annotations

import math


class SolverIncomplete(RuntimeError):
    """Raised when the node cap is exceeded (should not happen in practice)."""


def _ceil_div(a, b):
    # b > 0
    return -((-a) // b)


def _floor_div(a, b):
    # b > 0
    return a // b


class Solver:
    def __init__(self, node_cap=200000):
        self.domains = {}        # var -> (lo, hi)
        self.lineqs = []         # (coeffs: dict var->int, rhs: int)
        self.lookups = []        # (x, y, pairs)
        self.signexts = []       # (x, y, bits)
        self.leaf_checks = []    # callable(assignment) -> bool
        self.node_cap = node_cap
        self._fresh = 0

    # -- construction ------------------------------------------------------

    def add_var(self, name, lo, hi):
        assert lo <= hi, "empty initial domain for %s" % name
        if name in self.domains:
            old_lo, old_hi = self.domains[name]
            lo, hi = max(lo, old_lo), min(hi, old_hi)
            if lo > hi:
                raise ValueError("inconsistent domain for %s" % name)
        self.domains[name] = (lo, hi)
        return name

    def fresh_var(self, lo, hi, prefix="$aux"):
        self._fresh += 1
        return self.add_var("%s%d" % (prefix, self._fresh), lo, hi)

    def add_lineq(self, coeffs, rhs):
        coeffs = {v: c for v, c in coeffs.items() if c != 0}
        if not coeffs:
            if rhs != 0:
                self.domains["$infeasible"] = (1, 0)
            return
        for v in coeffs:
            assert v in self.domains, "unknown var %s" % v
        self.lineqs.append((coeffs, rhs))

    def add_lookup(self, x, y, pairs):
        assert pairs, "empty lookup relation"
        self.lookups.append((x, y, tuple(pairs)))

    def add_sign_extend(self, x, y, bits):
        self.signexts.append((x, y, bits))

    def add_leaf_check(self, fn):
        self.leaf_checks.append(fn)

    # -- propagation -------------------------------------------------------

    def _propagate_lineq(self, domains, coeffs, rhs):
        # gcd test: sum(c_i x_i) = rhs has integer solutions only if
        # gcd(coeffs) divides rhs — catches parity-style infeasibility
        # without enumerating huge domains
        gcd = 0
        for c in coeffs.values():
            gcd = math.gcd(gcd, abs(c))
        if gcd > 1 and rhs % gcd != 0:
            return None
        changed = False
        for k, ck in coeffs.items():
            rest_lo = 0
            rest_hi = 0
            for v, cv in coeffs.items():
                if v == k:
                    continue
                lo, hi = domains[v]
                if cv > 0:
                    rest_lo += cv * lo
                    rest_hi += cv * hi
                else:
                    rest_lo += cv * hi
                    rest_hi += cv * lo
            # ck * x_k in [rhs - rest_hi, rhs - rest_lo]
            bound_lo = rhs - rest_hi
            bound_hi = rhs - rest_lo
            if ck > 0:
                new_lo = _ceil_div(bound_lo, ck)
                new_hi = _floor_div(bound_hi, ck)
            else:
                # ck < 0: x >= bound_hi/ck and x <= bound_lo/ck (flipped)
                new_lo = -(bound_hi // (-ck))
                new_hi = (-bound_lo) // (-ck)
            lo, hi = domains[k]
            lo, hi = max(lo, new_lo), min(hi, new_hi)
            if lo > hi:
                return None
            if (lo, hi) != domains[k]:
                domains[k] = (lo, hi)
                changed = True
        return changed

    def _propagate_lookup(self, domains, x, y, pairs):
        xlo, xhi = domains[x]
        ylo, yhi = domains[y]
        fwd = [(a, b) for a, b in pairs if xlo <= a <= xhi]
        if not fwd:
            return None
        bwd = [(a, b) for a, b in fwd if ylo <= b <= yhi]
        if not bwd:
            return None
        new = (max(xlo, min(a for a, _ in bwd)),
               min(xhi, max(a for a, _ in bwd)))
        changed = new != (xlo, xhi)
        domains[x] = new
        new = (max(ylo, min(b for _, b in bwd)),
               min(yhi, max(b for _, b in bwd)))
        changed = changed or new != (ylo, yhi)
        domains[y] = new
        return changed

    def _propagate_signext(self, domains, x, y, bits):
        m = 1 << (bits - 1)
        full = 1 << bits
        xlo, xhi = domains[x]
        xlo, xhi = max(xlo, 0), min(xhi, full - 1)
        if xlo > xhi:
            return None
        # forward image: [xlo,xhi] split at m, upper half shifted by -full
        images = []
        if xlo <= min(xhi, m - 1):
            images.append((xlo, min(xhi, m - 1)))
        if max(xlo, m) <= xhi:
            images.append((max(xlo, m) - full, xhi - full))
        ylo, yhi = domains[y]
        nylo = max(ylo, min(a for a, _ in images))
        nyhi = min(yhi, max(b for _, b in images))
        if nylo > nyhi:
            return None
        # inverse image of [nylo, nyhi]
        pre = []
        lo, hi = max(nylo, 0), min(nyhi, m - 1)
        if lo <= hi:
            pre.append((lo, hi))
        lo, hi = max(nylo, -m), min(nyhi, -1)
        if lo <= hi:
            pre.append((lo + full, hi + full))
        if not pre:
            return None
        nxlo = max(xlo, min(a for a, _ in pre))
        nxhi = min(xhi, max(b for _, b in pre))
        if nxlo > nxhi:
            return None
        changed = False
        if (nxlo, nxhi) != domains[x]:
            domains[x] = (nxlo, nxhi)
            changed = True
        if (nylo, nyhi) != domains[y]:
            domains[y] = (nylo, nyhi)
            changed = True
        return changed

    def _propagate(self, domains):
        domains = dict(domains)
        for _ in range(10000):
            changed = False
            for coeffs, rhs in self.lineqs:
                res = self._propagate_lineq(domains, coeffs, rhs)
                if res is None:
                    return None
                changed = changed or res
            for x, y, pairs in self.lookups:
                res = self._propagate_lookup(domains, x, y, pairs)
                if res is None:
                    return None
                changed = changed or res
            for x, y, bits in self.signexts:
                res = self._propagate_signext(domains, x, y, bits)
                if res is None:
                    return None
                changed = changed or res
            if not changed:
                return domains
        raise SolverIncomplete("propagation did not converge")

    # -- search ------------------------------------------------------------

    def solve(self):
        """Return an assignment dict, or None if unsatisfiable."""
        nodes = [0]

        def rec(domains):
            nodes[0] += 1
            if nodes[0] > self.node_cap:
                raise SolverIncomplete("node cap exceeded")
            domains = self._propagate(domains)
            if domains is None:
                return None
            widest = None
            width = -1
            for v, (lo, hi) in domains.items():
                if hi - lo > width:
                    widest, width = v, hi - lo
            if width == 0:
                assignment = {v: lo for v, (lo, hi) in domains.items()}
                for check in self.leaf_checks:
                    if not check(assignment):
                        return None
                return assignment
            lo, hi = domains[widest]
            mid = (lo + hi) // 2
            left = dict(domains)
            left[widest] = (lo, mid)
            result = rec(left)
            if result is not None:
                return result
            right = dict(domains)
            right[widest] = (mid + 1, hi)
            return rec(right)

        return rec(self.domains)
