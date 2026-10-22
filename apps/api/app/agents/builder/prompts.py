"""Builder prompts: write a Dockerfile from Scout's AppSpec, or fix one after a failed build.

Prompts stay small (Super reads them fine, but every token costs): the spec, a capped file list,
short excerpts of the files that decide how to build, and for a fix the failing step, the error
lines, and the end of the log.
"""

from pathlib import Path

from app.agents.scout.appspec import AppSpec
from app.agents.scout.repo_view import RepoView

MAX_FILES = 60
EXCERPT_CHARS = 1_500
LOG_CHARS = 3_000
# Files that decide how an app installs, builds, and starts.
BUILD_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
    "Procfile",
    "runtime.txt",
    ".python-version",
    "package.json",
    ".nvmrc",
    "go.mod",
    "Dockerfile",
    "app.py",
    "wsgi.py",
    "main.py",
    "manage.py",
    "server.js",
    "index.js",
    # Java (Maven, Gradle)
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.properties",
    "src/main/resources/application.properties",
    "src/main/resources/application.yml",
    # Ruby
    "Gemfile",
    "Gemfile.lock",
    ".ruby-version",
    "config.ru",
    # Rust
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain.toml",
    # PHP
    "composer.json",
    "composer.lock",
    "index.php",
    "public/index.php",
    # .NET (projects and solutions are matched by suffix: BUILD_SUFFIXES)
    "global.json",
    "Directory.Build.props",
)
# .NET projects and solutions are named after the app; at most this deep (src/Api/Api.csproj).
BUILD_SUFFIXES = (".csproj", ".fsproj", ".sln")
BUILD_SUFFIX_DEPTH = 3

# Dockerfile guidance per language (AppSpec.language), added to the prompt for that language
# only. Multi-stage where there is a compile step, pinned major versions, non-root where the
# base image allows it.
LANGUAGE_GUIDANCE = {
    "java": """\
- Multi-stage. Build with maven:3-eclipse-temurin-<java> (Maven) or gradle:8-jdk<java> \
(Gradle), using ./mvnw or ./gradlew when the repo has one. Copy pom.xml or the Gradle files \
first and resolve dependencies, then copy src and package with tests skipped.
- Run on eclipse-temurin:<java>-jre (default 21). Copy only what runs: the Spring Boot fat \
jar (not *-plain.jar), or the whole quarkus-app folder for Quarkus. Create a non-root user \
(useradd) and switch to it with USER.
- Spring Boot and Quarkus serve on 8080. CMD ["java", "-jar", ...].""",
    "ruby": """\
- Base ruby:<version>-slim (default 3.3). In a build stage install build-essential and the \
headers the Gemfile needs (libpq-dev for pg, default-libmysqlclient-dev for mysql2); keep \
only the runtime libraries (libpq5) in the final stage.
- Copy Gemfile and Gemfile.lock first, `bundle config set --local without 'development \
test'`, then bundle install.
- Rails: ENV RAILS_ENV=production, precompile assets with SECRET_KEY_BASE_DUMMY=1 when there \
are assets, start `bundle exec rails server -b 0.0.0.0 -p 3000`. Sinatra or Rack: rackup \
or puma bound to 0.0.0.0.
- Create a non-root user and USER it (chown the app's tmp and log folders).""",
    "rust": """\
- Multi-stage. Build on rust:<version>-slim-bookworm (default 1): copy Cargo.toml and \
Cargo.lock first, build the dependencies once with a stub src/main.rs for the cache, then \
copy src and `cargo build --release`. Install pkg-config and libssl-dev if a crate needs \
OpenSSL.
- Run on debian:bookworm-slim with ca-certificates (and libssl3 when OpenSSL is used); copy \
only the binary from target/release. Create a non-root user and USER it.
- The binary must bind 0.0.0.0 on the app's port (axum 3000, actix-web 8080). Rocket: set \
ENV ROCKET_ADDRESS=0.0.0.0 and ROCKET_PORT.""",
    "php": """\
- Base php:<version>-apache (default 8.3), serving on port 80; Apache runs PHP as www-data. \
Install the extensions composer.json needs with docker-php-ext-install (pdo_pgsql needs \
libpq-dev, zip needs libzip-dev) and `a2enmod rewrite` for Laravel and Slim.
- Get composer with `COPY --from=composer:2 /usr/bin/composer /usr/bin/composer`; copy \
composer.json and composer.lock first, run `composer install --no-dev --no-scripts \
--no-autoloader --no-interaction`, then copy the code and `composer dump-autoload \
--optimize`.
- Laravel and Slim: point the DocumentRoot at public/ (sed the apache2 site config) and \
chown storage and bootstrap/cache to www-data. Plain PHP: the code goes in /var/www/html.""",
    "dotnet": """\
- Multi-stage. Restore and publish on mcr.microsoft.com/dotnet/sdk:<version> (default 8.0): \
copy the .csproj files (and the .sln) first, `dotnet restore` the app's project, then copy \
the rest and `dotnet publish <project> -c Release -o /app/publish`.
- Run on mcr.microsoft.com/dotnet/aspnet:<version> (the same major version). .NET 8 images \
listen on 8080 and have a non-root user: USER app. ENTRYPOINT ["dotnet", "<Assembly>.dll"].""",
}

WRITE_SYSTEM = """You are Builder, part of Groundcrew. You write a Dockerfile that builds and \
runs the app described below, for Linux x86_64, in a sandbox with network access.

Rules:
- Use an official, slim base image that matches the runtime version, with a pinned major \
version (for example python:3.12-slim, node:22-slim). Compiled apps (Java, Rust, .NET, Go) \
build in one stage and run in a smaller runtime stage.
- Install dependencies from the app's own manifest (requirements.txt, package.json, go.mod, \
pom.xml, Gemfile, Cargo.toml, composer.json, *.csproj) before copying the rest of the code, \
so the layer caches.
- Run as a non-root user when the base image allows it.
- Start the app with its production server and bind to 0.0.0.0 on the app's port (EXPOSE it). \
No dev servers, no --reload.
- CMD and ENTRYPOINT in exec form (["app", "--port", "8080"]) get no variable expansion: write \
the port as a number, or use shell form with exec (CMD exec app --port ${PORT:-8080}).
- No secrets, no ARG or ENV with credentials; env vars come at run time.
- Do not download and run scripts from the internet. Keep it short.
Return the whole Dockerfile."""

FIX_SYSTEM = """You are Builder, part of Groundcrew. A Docker build in the sandbox failed. Fix \
the Dockerfile so the build passes and the app still starts with its production server on \
0.0.0.0 and the same port.

Rules:
- Read the failing step and the error lines. Fix the cause, not the symptom (for example, a \
Python package that compiles C code needs gcc and the library's dev headers).
- Change as little as possible; keep everything that worked.
- Do not repeat a fix that already failed (see earlier attempts).
- When the failing step is the smoke test, the image built but the app did not start or \
answer: read its output and fix how it runs (CMD or ENTRYPOINT, files and installed packages \
in the final stage, the port it listens on, binding 0.0.0.0).
- Never set the app's env vars in the Dockerfile, not even placeholders (DATABASE_URL, API keys): \
they come from the deploy at run time, and Groundcrew removes such ENV lines.
- No secrets, no ARG or ENV with credentials. Do not download and run scripts from the internet.
Return the whole corrected Dockerfile, the lines you changed, and the kind of fix."""


def spec_text(spec: AppSpec, port: int) -> str:
    env = ", ".join(
        f"{v.name}{' (secret)' if v.secret else ''}{' (required)' if v.required else ''}"
        for v in spec.env_vars
    )
    rows = [
        ("language", spec.language),
        ("framework", spec.framework),
        ("runtime version", spec.runtime_version),
        ("package manager", spec.package_manager),
        ("install", spec.install_cmd),
        ("build", spec.build_cmd),
        ("start", spec.start_cmd),
        ("port", port),
        ("health path", spec.health_path),
        ("database", spec.database),
        ("env vars", env or None),
    ]
    return "\n".join(f"- {name}: {value}" for name, value in rows if value not in (None, ""))


def build_files(files: list[str]) -> list[str]:
    """The files whose excerpts the model sees: BUILD_FILES in order, then .NET projects."""
    present = set(files)
    named = [name for name in BUILD_FILES if name in present]
    projects = [
        f for f in files if f.endswith(BUILD_SUFFIXES) and f.count("/") <= BUILD_SUFFIX_DEPTH
    ]
    return named + projects


def guidance_text(spec: AppSpec) -> str:
    guidance = LANGUAGE_GUIDANCE.get(str(spec.language or ""))
    return f"\n\nDockerfile notes for {spec.language}:\n{guidance}" if guidance else ""


def files_text(root: Path) -> str:
    view = RepoView(root)
    files = view.files
    listed = "\n".join(files[:MAX_FILES])
    more = f"\n... and {len(files) - MAX_FILES} more" if len(files) > MAX_FILES else ""
    excerpts = []
    for name in build_files(files):
        if (text := view.text(name)) is not None:
            excerpts.append(f"--- {name}\n{text[:EXCERPT_CHARS]}")
    return f"Files:\n{listed}{more}\n\n" + "\n\n".join(excerpts)


def write_messages(root: Path, spec: AppSpec, port: int) -> list[dict[str, str]]:
    user = f"App:\n{spec_text(spec, port)}{guidance_text(spec)}\n\n{files_text(root)}"
    return [{"role": "system", "content": WRITE_SYSTEM}, {"role": "user", "content": user}]


def fix_messages(
    root: Path,
    spec: AppSpec,
    port: int,
    dockerfile: str,
    failing_step: str | None,
    error_lines: list[str],
    log_tail: str,
    earlier: list[str],
) -> list[dict[str, str]]:
    numbered = "\n".join(f"{i:>3} {line}" for i, line in enumerate(dockerfile.splitlines(), 1))
    history = "\n".join(earlier) or "none"
    user = (
        f"App:\n{spec_text(spec, port)}{guidance_text(spec)}\n\n"
        f"Dockerfile that failed:\n{numbered}\n\n"
        f"Failing step: {failing_step or 'unknown'}\n"
        f"Error lines:\n" + "\n".join(error_lines[:12]) + "\n\n"
        f"End of the build log:\n{log_tail[-LOG_CHARS:]}\n\n"
        f"Earlier attempts:\n{history}\n\n{files_text(root)}"
    )
    return [{"role": "system", "content": FIX_SYSTEM}, {"role": "user", "content": user}]
