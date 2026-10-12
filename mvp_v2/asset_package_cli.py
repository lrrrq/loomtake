from __future__ import annotations

import argparse
import json

from .asset_package import (
    bundle_asset_package_v2,
    create_asset_package,
    search_asset_package,
    update_asset_package_index,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M+ 可插拔资产包")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("package_dir")
    create.add_argument("--package-id", required=True)
    create.add_argument("--source-root", required=True)
    create.add_argument("--media-root", action="append", default=[])
    create.add_argument("--metadata-root", action="append", default=[])
    create.add_argument("--category-id", default="launch")
    update = sub.add_parser("update")
    update.add_argument("package")
    bundle = sub.add_parser("bundle-v2")
    bundle.add_argument("package")
    bundle.add_argument("target_dir")
    bundle.add_argument("--package-version", type=int, default=1)
    search = sub.add_parser("search")
    search.add_argument("package")
    search.add_argument("query")
    search.add_argument("--kind", choices=("script", "media"), default="script")
    search.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    if args.command == "create":
        manifest = create_asset_package(
            args.package_dir,
            package_id=args.package_id,
            source_root=args.source_root,
            media_roots=args.media_root,
            metadata_roots=args.metadata_root,
            category_id=args.category_id,
        )
        print(manifest)
    elif args.command == "update":
        print(json.dumps(update_asset_package_index(args.package), ensure_ascii=False, indent=2))
    elif args.command == "bundle-v2":
        print(bundle_asset_package_v2(
            args.package,
            args.target_dir,
            package_version=args.package_version,
        ))
    else:
        print(json.dumps(search_asset_package(args.package, args.query, kind=args.kind, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
