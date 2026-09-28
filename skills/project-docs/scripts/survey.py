#!/usr/bin/env python3
"""Print a quick inventory of a repository to start the docs survey.

Usage: survey.py <repo-root>

Lists stack, tree with file counts, existing docs, entry points, persistence, messaging,
HTTP clients, config, CI, containers, tests and recent git history. Facts only; the
agent still reads the code to understand them.
"""
import collections
import os
import re
import subprocess
import sys

SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea",
             ".vscode", "__pycache__", ".venv", "venv", ".next", "coverage", ".angular"}
SOURCE_EXT = {".java", ".kt", ".scala", ".groovy", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".py",
              ".go", ".rs", ".cs", ".rb", ".php", ".swift", ".m", ".dart", ".sql", ".vue",
              ".svelte", ".html", ".scss", ".css"}
BUILD_FILES = ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json",
               "go.mod", "Cargo.toml", "pyproject.toml", "requirements.txt", "setup.py",
               "Gemfile", "composer.json", "project.yml", "Package.swift", "pubspec.yaml",
               "angular.json", "nx.json", "Makefile", "mvnw", "gradlew"]

PATTERNS = {
    "HTTP entry points": [
        r"@(RestController|Controller)\b", r"@(Get|Post|Put|Delete|Patch|Request)Mapping\(",
        r"@Path\(\"", r"\b(app|router)\.(get|post|put|delete|patch)\(",
        r"@(app|router)\.(get|post|put|delete|patch)\(", r"\bpath\(['\"]",
        r"http\.HandleFunc\(", r"@Controller\(['\"]"],
    "Message consumers/producers": [
        r"@(KafkaListener|RabbitListener|JmsListener|SqsListener|StreamListener)\b",
        r"KafkaTemplate|RabbitTemplate|JmsTemplate|StreamBridge"],
    "Schedulers": [r"@Scheduled\(", r"@SchedulerLock\(", r"cron\s*[:=]"],
    "HTTP clients": [r"@FeignClient\(", r"\bRestTemplate\b", r"\bWebClient\b", r"\bRestClient\b",
                     r"HttpClient\.newHttpClient", r"\baxios\.", r"HttpClient\b.*inject",
                     r"requests\.(get|post)\("],
    "Persistence": [r"@Entity\b", r"@Table\(", r"extends (Jpa|Crud|Mongo|R2dbc)Repository",
                    r"@Query\(", r"JdbcTemplate", r"@Document\("],
    "Configuration binding": [r"@ConfigurationProperties\(", r"@Value\(\"\$\{"],
    "Security": [r"SecurityFilterChain|WebSecurityConfigurerAdapter", r"@PreAuthorize\(",
                 r"OncePerRequestFilter", r"KeyStore\b|\.jks\b|\.p12\b"],
    "Transactions/locking": [r"@Transactional\b", r"@Version\b", r"@Lock\(", r"FOR UPDATE"],
    "Error handling": [r"@(RestControllerAdvice|ControllerAdvice|ExceptionHandler)\b"],
}


def git_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True,
                             check=True).stdout.splitlines()
        if out:
            return [f for f in out if not any(p in SKIP_DIRS for p in f.split("/"))]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, name), root))
    return files


def section(title):
    print(f"\n## {title}")


def read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = os.path.abspath(sys.argv[1])
    files = git_files(root)
    print(f"# Survey of {root}\n{len(files)} files")

    section("Existing docs (stop gate — any hit means: plan and ask first)")
    doc_hits = [f for f in files if re.match(r"(docs?|documentation)/", f, re.I)
                or f in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md", "CONTEXT-MAP.md")
                or (f.lower().endswith(".md") and "/" not in f
                    and f.upper() not in ("README.MD", "CHANGELOG.MD", "LICENSE.MD"))]
    print("\n".join(f"- {f}" for f in doc_hits[:60]) or "- none")
    if len(doc_hits) > 60:
        print(f"- … {len(doc_hits) - 60} more")

    section("Build and stack files")
    for f in files:
        if os.path.basename(f) in BUILD_FILES and f.count("/") <= 2:
            print(f"- {f}")
    pom = read(root, "pom.xml")
    for tag in ["parent>\\s*<groupId>[^<]*</groupId>\\s*<artifactId>([^<]*)</artifactId>\\s*<version>([^<]*)",
                "<java.version>([^<]*)", "<maven.compiler.release>([^<]*)"]:
        m = re.search(tag, pom)
        if m:
            print(f"- pom: {' '.join(m.groups())}")
    if pom:
        deps = re.findall(r"<artifactId>([^<]+)</artifactId>", pom)
        print(f"- pom artifactIds: {', '.join(sorted(set(deps))[:60])}")
    pkg = read(root, "package.json")
    if pkg:
        m = re.search(r'"scripts"\s*:\s*\{([^}]*)\}', pkg, re.S)
        if m:
            print("- package.json scripts:" + m.group(1).replace("\n", "\n   "))

    section("Languages (by extension)")
    ext = collections.Counter(os.path.splitext(f)[1] for f in files)
    print(", ".join(f"{e or '(none)'}={n}" for e, n in ext.most_common(15)))

    section("Tree (directories with file counts, depth ≤ 6)")
    counts = collections.Counter()
    for f in files:
        d = os.path.dirname(f)
        if d and d.count("/") <= 6:
            counts[d] += 1
    for d in sorted(counts):
        if counts[d] >= 1 and not re.search(r"(xsd|schemas?)/", d, re.I):
            print(f"{'  ' * d.count('/')}{d.split('/')[-1]}/ ({counts[d]})  [{d}]")

    section("Config, containers, CI")
    for f in files:
        base = os.path.basename(f)
        if (re.match(r"(application|bootstrap)[-\w]*\.(ya?ml|properties)$", base)
                or base in (".env.example", ".env.sample", "docker-compose.yml",
                            "docker-compose.yaml", "compose.yml", "Jenkinsfile",
                            "bitbucket-pipelines.yml", ".gitlab-ci.yml", "azure-pipelines.yml",
                            ".editorconfig", "checkstyle.xml", ".eslintrc.json", ".prettierrc",
                            "logback-spring.xml", "logback.xml")
                or base.lower().startswith("dockerfile")
                or f.startswith(".github/workflows/")
                or re.search(r"\.(jks|p12|pem|crt|cer|keystore)$", base)):
            print(f"- {f}")

    section("Migrations and SQL")
    mig = [f for f in files if re.search(r"(liquibase|flyway|migration|changelog|db/)", f, re.I)
           or f.endswith(".sql")]
    for d, n in collections.Counter(os.path.dirname(f) for f in mig).most_common(20):
        print(f"- {d}/ ({n} files)")

    section("Tests")
    tests = [f for f in files if re.search(r"(^|/)(src/test|tests?|__tests__|spec)/", f)
             or re.search(r"(Test|Tests|IT|\.spec|\.test|_test)\.\w+$", f)]
    print(f"{len(tests)} test files")
    for f in tests[:25]:
        print(f"- {f}")

    section("Code signals (file: first matching line)")
    sources = [f for f in files if os.path.splitext(f)[1] in SOURCE_EXT]
    texts = {f: read(root, f) for f in sources}
    for title, pats in PATTERNS.items():
        rx = re.compile("|".join(pats))
        hits = []
        for f, text in texts.items():
            for line in text.splitlines():
                if rx.search(line):
                    hits.append(f"- {f}: {line.strip()[:110]}")
                    break
        print(f"\n### {title} ({len(hits)} files)")
        print("\n".join(hits[:40]) or "- none")
        if len(hits) > 40:
            print(f"- … {len(hits) - 40} more")

    section("Enums (candidate domain terms)")
    enum_rx = re.compile(r"\benum\s+(\w+)")
    enums = sorted({m for t in texts.values() for m in enum_rx.findall(t)})
    print(", ".join(enums[:120]) or "none")

    section("Recent git history")
    try:
        log = subprocess.run(["git", "-C", root, "log", "--oneline", "-30"], capture_output=True,
                             text=True, check=True).stdout
        print(log or "no commits")
        br = subprocess.run(["git", "-C", root, "branch", "--show-current"], capture_output=True,
                            text=True).stdout.strip()
        print(f"current branch: {br}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("not a git repo")


if __name__ == "__main__":
    main()
