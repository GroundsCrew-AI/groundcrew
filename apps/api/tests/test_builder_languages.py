"""Java, Ruby, Rust, PHP, and .NET apps without a Dockerfile: Scout's rules (golden fixtures in
demo/fixtures plus variants here) and what Builder's prompt shows the model."""

from pathlib import Path
from typing import Any

import pytest

from app.agents.builder.builder import app_port
from app.agents.builder.prompts import BUILD_FILES, fix_messages, write_messages
from app.agents.scout.appspec import AppSpec
from app.agents.scout.detectors import detect, find_subpath
from app.agents.scout.repo_view import RepoView
from app.agents.scout.scout import merge

FIXTURES = Path(__file__).resolve().parents[3] / "demo" / "fixtures"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _values(root: Path) -> dict[str, Any]:
    return {k: f.value for k, f in detect(RepoView(root)).values.items()}


def _spec(root: Path) -> AppSpec:
    view = RepoView(root)
    return merge(view, detect(view), None)


# --- Scout: variants of each ecosystem ---------------------------------------------------------


def test_gradle_quarkus(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "build.gradle.kts",
        'plugins {\n    java\n    id("io.quarkus")\n}\n'
        "dependencies {\n"
        '    implementation("io.quarkus:quarkus-rest")\n'
        '    implementation("io.quarkus:quarkus-smallrye-health")\n'
        '    implementation("io.quarkus:quarkus-jdbc-mysql")\n'
        '    runtimeOnly("com.mysql:mysql-connector-j:9.0.0")\n'
        "}\n"
        "java {\n    toolchain { languageVersion = JavaLanguageVersion.of(17) }\n}\n",
    )
    _write(tmp_path, "gradlew", "#!/bin/sh\n")
    _write(tmp_path, "src/main/resources/application.properties", "quarkus.http.port=8081\n")
    v = _values(tmp_path)
    assert v["language"] == "java" and v["framework"] == "quarkus"
    assert v["package_manager"] == "gradle" and v["runtime_version"] == "17"
    assert v["build_cmd"] == "./gradlew build -x test"
    assert v["start_cmd"] == "java -jar build/quarkus-app/quarkus-run.jar"
    assert v["port"] == 8081  # the app's config beats the convention
    assert v["health_path"] == "/q/health" and v["database"] == "mysql"


def test_gradle_spring_jar_name_and_yaml_port(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "build.gradle",
        "plugins {\n  id 'org.springframework.boot' version '3.3.4'\n}\n"
        "version = '1.2.0'\nsourceCompatibility = '21'\n"
        "dependencies {\n  implementation 'org.springframework.boot:spring-boot-starter-web'\n}\n",
    )
    _write(tmp_path, "settings.gradle", "rootProject.name = 'billing'\n")
    _write(tmp_path, "src/main/resources/application.yml", "server:\n  port: 9000\n")
    v = _values(tmp_path)
    assert v["framework"] == "spring" and v["runtime_version"] == "21"
    assert v["start_cmd"] == "java -jar build/libs/billing-1.2.0.jar"
    assert v["port"] == 9000 and "health_path" not in v


def test_maven_wrapper_and_final_name(tmp_path: Path) -> None:
    pom = (FIXTURES / "java-spring-maven" / "repo" / "pom.xml").read_text()
    _write(tmp_path, "pom.xml", pom.replace("<build>", "<build>\n    <finalName>app</finalName>"))
    _write(tmp_path, "mvnw", "#!/bin/sh\n")
    v = _values(tmp_path)
    assert v["install_cmd"] == "./mvnw -B dependency:go-offline"
    assert v["build_cmd"] == "./mvnw -B package -DskipTests"
    assert v["start_cmd"] == "java -jar target/app.jar"


@pytest.mark.parametrize(
    ("files", "framework", "start", "port"),
    [
        (
            {"Gemfile": 'gem "sinatra"\ngem "puma"\n', "config.ru": "run Sinatra::Application\n"},
            "sinatra",
            "bundle exec rackup --host 0.0.0.0 --port 9292",
            9292,
        ),
        (
            {"Gemfile": 'gem "rack"\n', "config.ru": "run ->(env) { [200, {}, ['ok']] }\n"},
            "rack",
            "bundle exec rackup --host 0.0.0.0 --port 9292",
            9292,
        ),
        (
            {"Gemfile": "gem 'sinatra'\n", "app.rb": "require 'sinatra'\nget('/') { 'hi' }\n"},
            "sinatra",
            "bundle exec ruby app.rb -o 0.0.0.0 -p 4567",
            4567,
        ),
    ],
)
def test_ruby_without_rails(
    tmp_path: Path, files: dict[str, str], framework: str, start: str, port: int
) -> None:
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    v = _values(tmp_path)
    assert (v["language"], v["framework"], v["start_cmd"], v["port"]) == (
        "ruby",
        framework,
        start,
        port,
    )
    assert v["package_manager"] == "bundler" and v["install_cmd"] == "bundle install"


def test_rails_with_package_json_stays_ruby(tmp_path: Path) -> None:
    _write(tmp_path, "Gemfile", 'gem "rails"\ngem "mysql2"\n')
    _write(tmp_path, "Gemfile.lock", "GEM\n")
    _write(tmp_path, ".ruby-version", "ruby-3.2.4\n")
    _write(tmp_path, "app/assets/config/manifest.js", "//= link_tree ../images\n")
    _write(tmp_path, "package.json", '{"dependencies": {"esbuild": "0.23.0"}}')
    _write(tmp_path, "yarn.lock", "\n")
    found = detect(RepoView(tmp_path))
    v = {k: f.value for k, f in found.values.items()}
    assert v["language"] == "ruby" and v["framework"] == "rails"
    assert v["package_manager"] == "bundler" and v["runtime_version"] == "3.2.4"
    assert v["build_cmd"] == "bundle exec rails assets:precompile"
    assert v["database"] == "mysql"
    assert any("front-end assets" in note for note in found.notes)


@pytest.mark.parametrize(
    ("crate", "framework", "port"),
    [('actix-web = "4"', "actix", 8080), ('rocket = "0.5"', "rocket", 8000)],
)
def test_rust_frameworks(tmp_path: Path, crate: str, framework: str, port: int) -> None:
    _write(
        tmp_path,
        "Cargo.toml",
        f'[package]\nname = "api"\n\n[[bin]]\nname = "server"\npath = "src/main.rs"\n\n'
        f'[dependencies]\n{crate}\nredis = "0.25"\n',
    )
    _write(tmp_path, "Cargo.lock", "version = 3\n")
    _write(tmp_path, "rust-toolchain.toml", '[toolchain]\nchannel = "1.79"\n')
    v = _values(tmp_path)
    assert (v["framework"], v["port"]) == (framework, port)
    assert v["start_cmd"] == "./target/release/server"
    assert v["build_cmd"] == "cargo build --release --locked"
    assert v["runtime_version"] == "1.79" and v["database"] == "redis"


def test_rust_port_in_code_beats_the_convention(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "api"\n[dependencies]\naxum = "0.7"\n')
    _write(
        tmp_path,
        "src/main.rs",
        'async fn main() {\n    let l = TcpListener::bind("0.0.0.0:7000").await;\n'
        '    let app = Router::new().route("/healthz", get(ok));\n}\n',
    )
    v = _values(tmp_path)
    assert v["port"] == 7000 and v["health_path"] == "/healthz"


def test_php_slim_and_plain(tmp_path: Path) -> None:
    slim = tmp_path / "slim"
    _write(slim, "composer.json", '{"require": {"php": ">=8.1", "slim/slim": "^4.0"}}')
    _write(slim, "composer.lock", "{}")
    v = _values(slim)
    assert (v["language"], v["framework"], v["port"]) == ("php", "slim", 80)
    assert v["package_manager"] == "composer" and v["runtime_version"] == "8.1"
    assert v["start_cmd"] == "apache2-foreground"

    plain = tmp_path / "plain"
    _write(plain, "index.php", "<?php echo 'hi';\n")
    v = _values(plain)
    assert (v["language"], v["framework"], v["port"]) == ("php", "none", 80)
    assert "install_cmd" not in v


def test_dotnet_project_at_the_root_on_net7(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Shop.csproj",
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n'
        "    <TargetFramework>net7.0</TargetFramework>\n"
        "    <AssemblyName>Shop.Web</AssemblyName>\n  </PropertyGroup>\n</Project>\n",
    )
    v = _values(tmp_path)
    assert v["framework"] == "aspnet" and v["runtime_version"] == "7.0"
    assert v["port"] == 80  # before .NET 8 the images listened on 80
    assert v["start_cmd"] == "dotnet Shop.Web.dll"
    assert v["install_cmd"] == "dotnet restore Shop.csproj"


def test_dotnet_picks_the_web_project_of_a_solution(tmp_path: Path) -> None:
    _write(tmp_path, "Shop.sln", "Microsoft Visual Studio Solution File\n")
    _write(tmp_path, "src/Shop.Domain/Shop.Domain.csproj", '<Project Sdk="Microsoft.NET.Sdk" />\n')
    _write(tmp_path, "src/Shop.Api/Shop.Api.csproj", '<Project Sdk="Microsoft.NET.Sdk.Web" />\n')
    assert _values(tmp_path)["start_cmd"] == "dotnet Shop.Api.dll"
    # The solution is the app root: no monorepo subpath.
    assert find_subpath(RepoView(tmp_path)) == (None, [])


def test_new_manifests_find_a_monorepo_subpath(tmp_path: Path) -> None:
    _write(tmp_path, "services/ledger/Cargo.toml", '[package]\nname = "ledger"\n')
    assert find_subpath(RepoView(tmp_path)) == ("services/ledger", ["services/ledger"])
    _write(tmp_path, "services/api/Api.csproj", '<Project Sdk="Microsoft.NET.Sdk.Web" />\n')
    assert find_subpath(RepoView(tmp_path)) == (None, ["services/api", "services/ledger"])


# --- Builder: files, port, and guidance in the prompt ------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "files", "port", "guidance"),
    [
        ("java-spring-maven", ["pom.xml"], 8080, "eclipse-temurin"),
        ("ruby-rails", ["Gemfile"], 3000, "ruby:<version>-slim"),
        ("rust-axum", ["Cargo.toml"], 3000, "debian:bookworm-slim"),
        ("php-laravel", ["composer.json"], 80, "php:<version>-apache"),
        (
            "dotnet-aspnet",
            ["Weather.sln", "src/Weather.Api/Weather.Api.csproj"],
            8080,
            "mcr.microsoft.com/dotnet/aspnet",
        ),
    ],
)
def test_write_prompt_has_the_manifests_port_and_guidance(
    fixture: str, files: list[str], port: int, guidance: str
) -> None:
    root = FIXTURES / fixture / "repo"
    spec = _spec(root)
    assert app_port(spec) == port
    system, user = write_messages(root, spec, app_port(spec))
    assert "non-root" in system["content"]
    for name in files:
        assert f"--- {name}\n" in user["content"], name
    assert f"- port: {port}\n" in user["content"]
    assert f"Dockerfile notes for {spec.language}:" in user["content"]
    assert guidance in user["content"]
    fix = fix_messages(root, spec, port, "FROM x\n", "RUN x", ["error"], "log", [])
    assert guidance in fix[1]["content"]


def test_lockfiles_are_build_files() -> None:
    for name in ("Gemfile.lock", "Cargo.lock", "composer.lock", "settings.gradle.kts"):
        assert name in BUILD_FILES


def test_python_prompt_has_no_language_notes() -> None:
    root = FIXTURES / "fastapi-pg" / "repo"
    _, user = write_messages(root, _spec(root), 8000)
    assert "Dockerfile notes" not in user["content"]


@pytest.mark.parametrize(
    ("language", "framework", "port"),
    [
        ("java", None, 8080),
        ("ruby", "sinatra", 4567),
        ("ruby", "rack", 9292),
        ("ruby", None, 3000),
        ("rust", "rocket", 8000),
        ("rust", "axum", 3000),
        ("rust", None, 8080),
        ("php", "laravel", 80),
        ("dotnet", "aspnet", 8080),
        ("python", None, 8000),
    ],
)
def test_default_ports(language: Any, framework: Any, port: int) -> None:
    assert app_port(AppSpec(language=language, framework=framework)) == port
