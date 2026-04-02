#!/usr/bin/env -S uv run --with-requirements ./cli/requirements.txt

import argparse
from dataclasses import dataclass
from os import path
import os
import re
import subprocess
import tempfile
from pathlib import Path
import yaml

ROOT_PATH = Path(__file__).parent.parent.resolve()
COMPONENTS_PATH = ROOT_PATH / "components"
CLUSTERS_PATH = ROOT_PATH / "config" / "clusters"
FLUX_PATH = ROOT_PATH / "flux"


@dataclass
class HelmRepository:
    url: str
    type: str

    @classmethod
    def from_dict(cls, data: dict) -> "HelmRepository":
        return cls(
            url=data["url"],
            type=data["type"],
        )


@dataclass
class HelmChart:
    name: str
    version: str

    @classmethod
    def from_dict(cls, data: dict) -> "HelmChart":
        return cls(
            name=data["name"],
            version=data["version"],
        )


@dataclass
class HelmRelease:
    name: str
    namespace: str

    @classmethod
    def from_dict(cls, data: dict, default_name: str) -> "HelmRelease":
        return cls(
            name=data["name"],
            namespace=data["namespace"],
        )


@dataclass
class HelmConfig:
    repository: HelmRepository
    chart: HelmChart
    release: HelmRelease

    @classmethod
    def from_dict(cls, data: dict) -> "HelmConfig":
        chart = HelmChart.from_dict(data["chart"])

        return cls(
            repository=HelmRepository.from_dict(data["repository"]),
            chart=chart,
            release=HelmRelease.from_dict(data["release"], default_name=chart.name),
        )


def gomplate_render(file_path: Path, cluster: str) -> str:
    defaults_path = ROOT_PATH / "config" / "defaults.yaml"
    cluster_path = ROOT_PATH / "config" / "clusters" / f"{cluster}.yaml"

    result = subprocess.run(
        [
            "gomplate",
            "--datasource",
            f"defaults={defaults_path.relative_to(os.getcwd())}",
            "--datasource",
            f"cluster={cluster_path.relative_to(os.getcwd())}",
            "--datasource",
            "config=merge:cluster|defaults",
            "--file",
            file_path.relative_to(os.getcwd()),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(result.stderr)
        raise Exception(f"Failed to render {file_path}")

    return result.stdout


def get_chart_reference(config: HelmConfig) -> str:
    if config.repository.type == "oci":
        return f"oci://{config.repository.url}/{config.chart.name}"

    raise Exception(f"Unsupported repository type: {config.repository.type}")


def helm_template(config: HelmConfig, values: dict) -> str:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".yaml") as values_file:
        values_file.write(yaml.dump(values))
        values_file.flush()

        result = subprocess.run(
            [
                "helm",
                "template",
                config.release.name,
                get_chart_reference(config),
                "--version",
                config.chart.version,
                "--namespace",
                config.release.namespace,
                "--values",
                values_file.name,
            ],
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        print(result.stderr)
        raise Exception(
            f"Failed to render Helm chart {config.chart.name}:{config.chart.version}"
        )

    return result.stdout


def render_yaml_dir(source_dir: Path, destination_dir: Path, cluster: str) -> list[Path]:
    rendered_files = []

    for file_path in sorted(source_dir.iterdir()):
        if file_path.is_file() and file_path.suffix in (".yaml", ".yml"):
            rendered = gomplate_render(file_path, cluster)
            rendered_path = destination_dir / file_path.name
            rendered_path.parent.mkdir(parents=True, exist_ok=True)
            rendered_path.write_text(rendered)
            rendered_files.append(rendered_path)

    return rendered_files


def main():
    parser = argparse.ArgumentParser()

    cluster_choices = sorted(
        path.name.split(".")[0] for path in CLUSTERS_PATH.iterdir() if not path.is_dir()
    )

    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument(
        "--cluster",
        required=True,
        choices=cluster_choices,
        help="Target cluster name",
    )

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument(
        "--cluster",
        required=True,
        choices=cluster_choices,
        help="Target cluster name",
    )

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument(
        "--cluster",
        required=True,
        choices=cluster_choices,
        help="Target cluster name",
    )
    render_parser.add_argument(
        "--component",
        required=True,
        choices=sorted(
            path.name for path in COMPONENTS_PATH.iterdir() if path.is_dir()
        ),
        help="Component directory name under components/",
    )

    args = parser.parse_args()

    if args.command == "setup":
        subprocess.run(
            ["kind", "create", "cluster", "--name", args.cluster], check=True
        )
        subprocess.run(["flux", "install"], check=True)
        with tempfile.TemporaryDirectory() as rendered_flux_dir:
            rendered_files = render_yaml_dir(
                FLUX_PATH, Path(rendered_flux_dir), args.cluster
            )
            if rendered_files:
                subprocess.run(
                    ["kubectl", "apply", "-f", rendered_flux_dir],
                    check=True,
                )
    elif args.command == "delete":
        subprocess.run(
            ["kind", "delete", "cluster", "--name", args.cluster], check=True
        )
    elif args.command == "render":
        helm_dir = COMPONENTS_PATH / args.component / "helm"
        if helm_dir.exists() and helm_dir.is_dir():
            helmConfig = HelmConfig.from_dict(
                yaml.safe_load(
                    gomplate_render(
                        helm_dir / "meta.yaml",
                        args.cluster,
                    )
                )
            )

            values = yaml.safe_load(
                gomplate_render(
                    helm_dir / "values.yaml",
                    args.cluster,
                )
            )

            rendered = helm_template(helmConfig, values)

            # Strip version labels
            rendered = re.sub(
                r"^[ \t]*app\.kubernetes\.io/version:.*\n?",
                "",
                rendered,
                flags=re.MULTILINE,
            )
            rendered = re.sub(
                r"^[ \t]*helm\.sh/chart:.*\n?", "", rendered, flags=re.MULTILINE
            )

            rendered_path = (
                ROOT_PATH
                / "rendered"
                / args.cluster
                / args.component
                / "helm"
                / "manifests.yaml"
            )

            rendered_path.parent.mkdir(parents=True, exist_ok=True)
            rendered_path.write_text(rendered)

            print(f"Rendered {rendered_path}")

        k8s_dir = COMPONENTS_PATH / args.component / "k8s"
        if k8s_dir.exists() and k8s_dir.is_dir():
            rendered_files = render_yaml_dir(
                k8s_dir,
                ROOT_PATH / "rendered" / args.cluster / args.component / "k8s",
                args.cluster,
            )
            for rendered_path in rendered_files:
                print(f"Rendered {rendered_path}")


if __name__ == "__main__":
    main()
