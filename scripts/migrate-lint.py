#!/usr/bin/env python3
"""Lint per-domain golang-migrate migrations against the DAG manifest.

Checks (see README for the full design):
  1. manifest sanity: paths exist, no table owned twice, depends_on is a DAG
  2. filename hygiene: NNNNNN_title.(up|down).sql, up/down pairs, versions
     contiguous 1..N per domain
  3. append-only vs a base ref: new versions must be greater than the max
     version already on the base (no renumbering history)
  4. table ownership: DDL only on owned tables; references only to tables
     owned by this domain or a transitive ancestor (else: DAG back-edge)

Exits non-zero listing every violation found.

With --print-order, validates the manifest, prints
`domain<TAB>path<TAB>tracking_table` in topological order and exits;
scripts/migrate-up.sh consumes this so lint and runner can never disagree
on ordering.
"""

import argparse
import bisect
import re
import subprocess
import sys
from pathlib import Path

import yaml

FILENAME_RE = re.compile(r"^(\d{6})_([A-Za-z0-9_]+)\.(up|down)\.sql$")
ALLOW_RE = re.compile(
    r"--\s*lint:allow\s+tables=([A-Za-z0-9_,\s]+?)\s+reason=\"([^\"]+)\""
)
COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/|#[^\n]*", re.DOTALL)

# Words a naive regex over FROM/JOIN/... might capture that are never tables.
REGEX_NOISE = {"select", "dual", "set", "where", "values"}


def norm(name: str) -> str:
    """Normalize a table identifier: strip quoting and schema prefix."""
    name = name.strip().strip("`\"'")
    if "." in name:
        name = name.split(".")[-1].strip().strip("`\"'")
    return name.lower()


class Linter:
    def __init__(self, repo_root: Path, manifest_path: Path):
        self.root = repo_root
        self.manifest_path = manifest_path
        self.violations = []
        self.domains = {}  # name -> {path, tracking_table, depends_on, owns_tables}
        self.owner = {}  # table -> domain
        self.order = []  # topological order
        self.ancestors = {}  # domain -> set of transitive ancestors

    def err(self, msg: str) -> None:
        self.violations.append(msg)

    # ---------------------------------------------------------------- manifest

    def load_manifest(self) -> bool:
        try:
            raw = yaml.safe_load(self.manifest_path.read_text())
        except (OSError, yaml.YAMLError) as e:
            self.err(f"{self.manifest_path}: cannot parse manifest: {e}")
            return False
        domains = (raw or {}).get("domains")
        if not isinstance(domains, dict) or not domains:
            self.err(f"{self.manifest_path}: expected a non-empty `domains:` mapping")
            return False

        for name, cfg in domains.items():
            if not isinstance(cfg, dict):
                self.err(f"{self.manifest_path}: domain `{name}` must be a mapping")
                continue
            missing = [k for k in ("path", "tracking_table", "owns_tables") if not cfg.get(k)]
            if missing:
                self.err(
                    f"{self.manifest_path}: domain `{name}` missing required "
                    f"key(s): {', '.join(missing)}"
                )
                continue
            path = Path(cfg["path"])
            if not (self.root / path).is_dir():
                self.err(f"{self.manifest_path}: domain `{name}` path does not exist: {path}")
            self.domains[name] = {
                "path": str(path),
                "tracking_table": cfg["tracking_table"],
                "depends_on": list(cfg.get("depends_on") or []),
                "owns_tables": {norm(t) for t in cfg["owns_tables"]},
            }
            for table in cfg["owns_tables"]:
                t = norm(table)
                if t in self.owner:
                    self.err(
                        f"{self.manifest_path}: table `{t}` owned by both "
                        f"`{self.owner[t]}` and `{name}`"
                    )
                else:
                    self.owner[t] = name

        for name, cfg in self.domains.items():
            for dep in cfg["depends_on"]:
                if dep not in self.domains:
                    self.err(
                        f"{self.manifest_path}: domain `{name}` depends on "
                        f"unknown domain `{dep}`"
                    )
        return bool(self.domains)

    def compute_topo_order(self) -> bool:
        """Kahn's algorithm with sorted tie-breaking for deterministic output."""
        indegree = {d: 0 for d in self.domains}
        children = {d: [] for d in self.domains}
        for name, cfg in self.domains.items():
            for dep in cfg["depends_on"]:
                if dep in self.domains:
                    indegree[name] += 1
                    children[dep].append(name)

        ready = sorted(d for d, n in indegree.items() if n == 0)
        order = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    bisect.insort(ready, child)

        if len(order) != len(self.domains):
            stuck = sorted(set(self.domains) - set(order))
            self.err(
                "depends_on edges contain a cycle involving: " + ", ".join(stuck)
            )
            return False

        self.order = order
        for name in order:
            anc = set()
            for dep in self.domains[name]["depends_on"]:
                if dep in self.ancestors:
                    anc.add(dep)
                    anc |= self.ancestors[dep]
            self.ancestors[name] = anc
        return True

    # ------------------------------------------------------------------ files

    def collect_files(self, domain: str):
        """Return {version: {'up': Path, 'down': Path}} and lint filenames."""
        dirpath = self.root / self.domains[domain]["path"]
        if not dirpath.is_dir():
            return {}
        versions = {}
        for f in sorted(dirpath.iterdir()):
            if f.name == ".gitkeep" or f.is_dir():
                continue
            m = FILENAME_RE.match(f.name)
            rel = f.relative_to(self.root)
            if not m:
                self.err(
                    f"{rel}: filename does not match NNNNNN_title.(up|down).sql"
                )
                continue
            version, _title, direction = int(m.group(1)), m.group(2), m.group(3)
            slot = versions.setdefault(version, {})
            if direction in slot:
                self.err(
                    f"{rel}: duplicate version {version:06d} in domain "
                    f"`{domain}` (also {slot[direction].name})"
                )
                continue
            slot[direction] = f

        for version in sorted(versions):
            slot = versions[version]
            if "up" not in slot:
                self.err(
                    f"{domain}: version {version:06d} has a .down.sql but no .up.sql"
                )
            if "down" not in slot:
                self.err(
                    f"{domain}: version {version:06d} has a .up.sql but no .down.sql"
                )

        if versions:
            expected = list(range(1, max(versions) + 1))
            missing = [v for v in expected if v not in versions]
            if missing:
                self.err(
                    f"{domain}: versions are not contiguous 1..{max(versions):06d}; "
                    "missing: " + ", ".join(f"{v:06d}" for v in missing)
                )
        return versions

    # ------------------------------------------------------------ append-only

    def check_append_only(self, base_ref: str, files_by_domain: dict) -> None:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", base_ref],
            cwd=self.root, capture_output=True, text=True,
        )
        if probe.returncode != 0:
            print(
                f"note: base ref `{base_ref}` not found; skipping append-only check",
                file=sys.stderr,
            )
            return

        for domain in self.order:
            path = self.domains[domain]["path"]
            out = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", base_ref, "--", path],
                cwd=self.root, capture_output=True, text=True, check=True,
            ).stdout
            base_files = set()
            base_versions = set()
            for line in out.splitlines():
                name = Path(line).name
                m = FILENAME_RE.match(name)
                if m:
                    base_files.add(name)
                    base_versions.add(int(m.group(1)))
            if not base_versions:
                continue
            base_max = max(base_versions)
            for version, slot in sorted(files_by_domain.get(domain, {}).items()):
                for f in slot.values():
                    if f.name in base_files:
                        continue
                    if version <= base_max:
                        self.err(
                            f"{f.relative_to(self.root)}: file is new relative "
                            f"to {base_ref} but its version {version:06d} is "
                            f"not greater than the base's max version "
                            f"{base_max:06d} — migration history is "
                            "append-only; renumber it"
                        )

    # -------------------------------------------------------------- ownership

    def analyze_sql(self, text: str):
        """Return (created, ddl_targets, references) table-name sets."""
        import sqlglot
        from sqlglot import exp

        try:
            statements = sqlglot.parse(text, read="mysql")
        except sqlglot.errors.ParseError:
            return self.regex_analyze(text)

        created, ddl, refs = set(), set(), set()
        alter_classes = tuple(
            c for c in (getattr(exp, "Alter", None), getattr(exp, "AlterTable", None)) if c
        )
        truncate_cls = getattr(exp, "TruncateTable", None)

        for stmt in statements:
            if stmt is None:
                continue
            if isinstance(stmt, exp.Command):
                # sqlglot recognized the statement but did not build a full
                # AST for it; fall back to regex for this statement only.
                c, d, r = self.regex_analyze(stmt.sql(dialect="mysql"))
                created |= c
                ddl |= d
                refs |= r
                continue

            tables = [norm(t.name) for t in stmt.find_all(exp.Table) if t.name]
            if not tables:
                continue
            if isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE":
                created.add(tables[0])
                refs |= set(tables[1:]) - {tables[0]}
            elif isinstance(stmt, exp.Create):
                # CREATE INDEX/VIEW/...: treat the underlying table as DDL target
                ddl |= set(tables)
            elif isinstance(stmt, (exp.Drop,) + alter_classes) or (
                truncate_cls and isinstance(stmt, truncate_cls)
            ):
                ddl.add(tables[0])
                refs |= set(tables[1:]) - {tables[0]}
            else:
                refs |= set(tables)
        return created, ddl, refs - created - ddl

    @staticmethod
    def regex_analyze(text: str):
        """Regex fallback when sqlglot cannot parse a statement."""
        sql = COMMENT_RE.sub(" ", text)
        ident = r"[`\"]?([A-Za-z0-9_$]+)"
        created = {
            norm(m.group(1))
            for m in re.finditer(
                r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + ident, sql, re.I
            )
        }
        ddl = set()
        for pattern in (
            r"\bALTER\s+TABLE\s+" + ident,
            r"\bTRUNCATE\s+(?:TABLE\s+)?" + ident,
            r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + ident,
            r"\bRENAME\s+TABLE\s+" + ident,
        ):
            ddl |= {norm(m.group(1)) for m in re.finditer(pattern, sql, re.I)}
        refs = {
            norm(m.group(1))
            for m in re.finditer(
                r"\b(?:FROM|JOIN|INTO|UPDATE|REFERENCES)\s+" + ident, sql, re.I
            )
        }
        refs -= REGEX_NOISE
        return created, ddl - created, refs - created - ddl

    def check_ownership(self, files_by_domain: dict) -> None:
        for domain in self.order:
            owns = self.domains[domain]["owns_tables"]
            ancestors = self.ancestors[domain]
            for version in sorted(files_by_domain.get(domain, {})):
                up = files_by_domain[domain][version].get("up")
                if up is None:
                    continue
                text = up.read_text()
                rel = up.relative_to(self.root)

                allowed = set()
                for m in ALLOW_RE.finditer(text):
                    allowed |= {norm(t) for t in m.group(1).split(",") if t.strip()}

                created, ddl, refs = self.analyze_sql(text)

                for t in sorted(created - allowed):
                    if t not in owns:
                        self.err(
                            f"{rel}: CREATE TABLE `{t}` but `{t}` is not in "
                            f"`{domain}`.owns_tables — register it or move the "
                            "migration to the owning domain"
                        )
                for t in sorted(ddl - allowed):
                    if t not in owns:
                        owner = self.owner.get(t)
                        detail = f"owned by `{owner}`" if owner else "not registered in any domain"
                        self.err(
                            f"{rel}: DDL on `{t}` which `{domain}` does not own ({detail})"
                        )
                for t in sorted(refs - allowed):
                    if t in owns:
                        continue
                    owner = self.owner.get(t)
                    if owner in ancestors:
                        continue
                    if owner is None:
                        self.err(
                            f"{rel}: references `{t}` which is not registered "
                            "in any domain's owns_tables"
                        )
                    else:
                        self.err(
                            f"{rel}: DAG back-edge: `{domain}` references `{t}` "
                            f"owned by `{owner}`, but `{owner}` is not an "
                            f"ancestor of `{domain}` (declared depends_on chain)"
                        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default="migrations/dag.yaml", help="path to the DAG manifest"
    )
    parser.add_argument(
        "--base",
        help="git ref to enforce append-only versioning against (e.g. origin/main)",
    )
    parser.add_argument(
        "--print-order",
        action="store_true",
        help="print `domain<TAB>path<TAB>tracking_table` in topological order and exit",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    repo_root = manifest_path.parent.parent

    linter = Linter(repo_root, manifest_path)
    if linter.load_manifest():
        linter.compute_topo_order()

    if args.print_order:
        if linter.violations:
            for v in linter.violations:
                print(f"VIOLATION: {v}", file=sys.stderr)
            return 1
        for domain in linter.order:
            cfg = linter.domains[domain]
            print(f"{domain}\t{cfg['path']}\t{cfg['tracking_table']}")
        return 0

    if linter.order:
        files_by_domain = {d: linter.collect_files(d) for d in linter.order}
        if args.base:
            linter.check_append_only(args.base, files_by_domain)
        linter.check_ownership(files_by_domain)

    if linter.violations:
        print(f"{len(linter.violations)} violation(s):\n", file=sys.stderr)
        for v in linter.violations:
            print(f"VIOLATION: {v}", file=sys.stderr)
        return 1

    print("migrate-lint: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
