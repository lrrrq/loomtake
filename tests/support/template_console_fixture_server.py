"""Isolated canonical template API wired into the existing anonymous Formal fixture."""
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry
from mvp_v2.script_type_adapter import SCRIPT_TYPE_METADATA
from tests.support import formal_console_fixture_server as base
from tests.test_mvp_v2_template_selection import _ready_template, SECOND_SEGMENTS

registry = LocalPackageRegistry(base.root / "template-registry.json")
publication = AssetPackagePublicationService(
    storage=LocalAssetStorage(base.root / "template-objects"),
    registry=registry,
    packages_root=base.root / "template-packages",
)
for metadata in SCRIPT_TYPE_METADATA:
    category = metadata.category_id
    folder = base.fixture_dir / "templates" / category
    path = folder / (metadata.display_name + "基础模板.xlsx")
    if not path.exists():
        _ready_template(folder, path.name)
    if not registry.list()["aliases"].get(category):
        record = publication.import_template(category_id=category, filename=path.name, content=path.read_bytes())
        registry.promote(category, record["package_id"], record["package_version"])
second = base.fixture_dir / "templates" / "new" / "匿名结构模板.xlsx"
if not second.exists():
    _ready_template(second.parent, second.name, SECOND_SEGMENTS)
base.generation.template_packages = publication

if __name__ == "__main__":
    base.uvicorn.run(base.app, host="127.0.0.1", port=base.TEST_PORT, log_level="warning")
