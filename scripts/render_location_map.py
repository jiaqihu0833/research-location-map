#!/usr/bin/env python3
"""Render authorized local GIS snapshots. No data download or installation.

Usage (only after execution is authorized):
    python render_location_map.py /absolute/path/config.json

Requires GeoPandas, Cartopy, Matplotlib, NumPy, pandas, Shapely, pyproj >= 3.5
with PROJ >= 9.2. Excel and DEM support additionally require an appropriate
pandas Excel engine and Rasterio, respectively. This source has NOT been run
as part of skill development. See references/ for the configuration contract.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import partial
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import warnings
import zipfile


class MapInputError(ValueError):
    """A problem that must be resolved without guessing or changing inputs."""


def require(condition, message):
    if not condition:
        raise MapInputError(message)


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def local_path(value, base, *, must_exist=True):
    require(isinstance(value, str) and value.strip(), "Expected a nonempty local path.")
    require(not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)
            and not value.startswith(("//", "\\\\", "/vsi"))
            and "\x00" not in value, f"Remote/virtual paths are not permitted: {value}")
    path = Path(value).expanduser()
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    if must_exist:
        require(path.is_file(), f"Local input file does not exist: {path}")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def bbox_valid(bbox, name):
    require(isinstance(bbox, list) and len(bbox) == 4
            and all(finite_number(v) for v in bbox), f"{name}: use [west,south,east,north].")
    w, s, e, n = bbox
    require(-180 <= w < e <= 180 and -90 < s < n < 90,
            f"{name}: invalid extent; polar/dateline-crossing extents need another workflow.")


def validate_config(cfg, base):
    require(cfg.get("schema_version") == "1.0", "schema_version must be '1.0'.")
    require(cfg.get("data_mode") in {"public", "user", "mixed"}, "Invalid data_mode.")
    require(cfg.get("map_type") in {"administrative", "terrain_hydrology", "transport_samples"},
            "Invalid map_type.")
    require(cfg.get("language") in {"zh", "en"}, "language must be zh or en.")
    require(isinstance(cfg.get("data_year"), str) and cfg["data_year"].strip(),
            "Record data_year; use an explicit 'unknown' if genuinely unavailable.")
    study = cfg.get("study_area", {})
    require(isinstance(study.get("name"), str) and study["name"].strip(), "study_area.name is required.")
    if study.get("expected_bbox_wgs84") is not None:
        bbox_valid(study["expected_bbox_wgs84"], "study_area.expected_bbox_wgs84")
    layout = cfg.get("layout", {})
    for key, lower, upper in (("width_mm", 60, 600), ("height_mm", 60, 600),
                              ("dpi", 72, 1200), ("font_size_pt", 5, 24)):
        val = layout.get(key)
        require(finite_number(val) and lower <= val <= upper,
                f"layout.{key} must be between {lower} and {upper}.")
    require(isinstance(layout.get("font_family"), str) and layout["font_family"].strip(),
            "Specify layout.font_family explicitly.")
    require(isinstance(cfg.get("caption"), str) and cfg["caption"].strip(),
            "caption must include the figure's required source/attribution wording; do not leave it empty.")
    outputs = cfg.get("outputs", {})
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", outputs.get("basename", "")) is not None,
            "outputs.basename must be a plain ASCII file stem.")
    formats = outputs.get("formats", [])
    require(isinstance(formats, list) and formats and len(set(formats)) == len(formats)
            and set(formats) <= {"png", "svg", "pdf"}, "Use unique png/svg/pdf output formats.")
    outdir = local_path(outputs.get("directory"), base, must_exist=False)
    require(not outdir.exists(), f"Output directory already exists; choose a new run directory: {outdir}")
    layers = cfg.get("layers", [])
    require(isinstance(layers, list) and layers, "Local layers are required; names alone cannot be rendered.")
    ids = [layer.get("id") for layer in layers]
    require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids),
            "Layer ids must be nonempty and unique.")
    require(study.get("boundary_layer") in ids, "study_area.boundary_layer must name a vector layer.")
    for layer in layers:
        ident = layer["id"]
        require(layer.get("kind") in {"vector", "points_table", "dem"}, f"{ident}: invalid kind.")
        require(layer.get("role") in {"context", "study_area", "roads", "water", "samples", "terrain"},
                f"{ident}: invalid role.")
        require(layer.get("coordinate_system") in {"WGS84", "CGCS2000", "other"},
                f"{ident}: identify coordinates; GCJ-02/BD-09 require a separate reviewed conversion.")
        local_path(layer.get("path"), base)
        if layer.get("source_crs") is not None:
            require(isinstance(layer["source_crs"], str), f"{ident}: source_crs must be a CRS string.")
        provenance = layer.get("provenance", {})
        for key in ("source", "version", "year", "license", "spatial_resolution", "acquired_at"):
            require(isinstance(provenance.get(key), str) and provenance[key].strip(),
                    f"{ident}: provenance.{key} is required; record 'unknown' rather than inventing it.")
        require(set(layer.get("style", {})) <= {"facecolor", "edgecolor", "color", "linewidth", "markersize", "alpha"},
                f"{ident}: unrecognized style field.")
        for key in ("linewidth", "markersize", "alpha"):
            if key in layer.get("style", {}):
                value = layer["style"][key]
                require(finite_number(value) and value >= 0, f"{ident}: invalid style.{key}.")
                if key == "alpha":
                    require(value <= 1, f"{ident}: alpha must be <= 1.")
        if layer["kind"] == "dem":
            require(layer["role"] == "terrain", f"{ident}: DEM role must be terrain.")
            require(layer.get("elevation_unit") in {"m", "ft"},
                    f"{ident}: DEM elevation_unit must explicitly be m or ft.")
        if layer.get("label") is not None:
            require(isinstance(layer["label"], str), f"{ident}: label must be text.")
    panels = cfg.get("panels", [])
    require(isinstance(panels, list) and len(panels) in {2, 3}, "Use two or three map panels.")
    pids = [panel.get("id") for panel in panels]
    require(all(isinstance(i, str) and i for i in pids) and len(set(pids)) == len(pids),
            "Panel ids must be nonempty and unique.")
    for panel in panels:
        ident = panel["id"]
        bbox_valid(panel.get("extent_wgs84"), f"{ident}.extent_wgs84")
        rect = panel.get("rect")
        require(isinstance(rect, list) and len(rect) == 4 and all(finite_number(v) for v in rect),
                f"{ident}.rect must be [left,bottom,width,height].")
        left, bottom, width, height = rect
        require(left >= 0 and bottom >= 0 and width > 0 and height > 0
                and left + width <= 1 and bottom + height <= 1, f"{ident}: rect exceeds figure.")
        require(re.fullmatch(r"EPSG:[0-9]+", panel.get("crs", "")) is not None,
                f"{ident}: this renderer supports projected EPSG codes only.")
        require(isinstance(panel.get("title", ""), str), f"{ident}: title must be text.")
        require(isinstance(panel.get("layers"), list) and panel["layers"]
                and set(panel["layers"]) <= set(ids), f"{ident}: unknown or empty layers.")
        require(isinstance(panel.get("locator_for", []), list)
                and set(panel.get("locator_for", [])) <= set(pids) - {ident}, f"{ident}: invalid locator target.")
        require(isinstance(panel.get("north_arrow", False), bool), f"{ident}: north_arrow must be boolean.")
        if panel.get("scale_bar_km") is not None:
            require(finite_number(panel["scale_bar_km"]) and panel["scale_bar_km"] > 0,
                    f"{ident}: scale_bar_km must be positive or null.")
    require(any(study["boundary_layer"] in p["layers"] for p in panels),
            "The study boundary must appear in at least one panel.")
    graph = {p["id"]: p.get("locator_for", []) for p in panels}
    visiting, visited = set(), set()

    def visit(node):
        require(node not in visiting, "locator_for references must not form a cycle.")
        if node in visited:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    used = {i for p in panels for i in p["layers"]}
    roles = {layer["role"] for layer in layers if layer["id"] in used}
    if cfg["map_type"] == "terrain_hydrology":
        require({"terrain", "water"} <= roles and any(l["kind"] == "dem" and l["id"] in used for l in layers),
                "terrain_hydrology requires a DEM terrain layer and a water layer in the panels.")
    if cfg["map_type"] == "transport_samples":
        require({"roads", "samples"} <= roles, "transport_samples requires roads and samples layers in the panels.")
    return outdir


def shapefile_members(names, selected=None, has_verified_crs=False):
    normalized = {n.lower(): n for n in names if not n.endswith("/")}
    require(len(normalized) == len([n for n in names if not n.endswith("/")]),
            "Case-insensitive duplicate ZIP members are ambiguous.")
    shapes = [n for n in normalized if n.endswith(".shp")]
    require(len(shapes) == 1 if selected is None else selected.lower() in shapes,
            "Provide one complete Shapefile per ZIP archive.")
    shape = selected.lower() if selected else shapes[0]
    stem = shape[:-4]
    required = (".shp", ".shx", ".dbf") + (() if has_verified_crs else (".prj",))
    require(all(stem + ext in normalized for ext in required),
            f"Incomplete Shapefile {shape}: .shp/.shx/.dbf are required; missing .prj needs a verified source_crs.")
    return normalized[shape]


def input_manifest(path, kind, has_verified_crs=False):
    files = [path]
    if path.suffix.lower() == ".shp":
        entries = list(path.parent.iterdir())
        shapefile_members([p.name for p in entries], path.name, has_verified_crs)
        sidecar_extensions = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fix"}
        files = sorted(p for p in entries if p.is_file() and p.stem.lower() == path.stem.lower()
                       and p.suffix.lower() in sidecar_extensions)
    if path.suffix.lower() == ".gpkg":
        require(not any(Path(str(path) + ext).exists() for ext in ("-wal", "-journal")),
                f"{path}: use a closed, consistent GeoPackage snapshot, without WAL/journal files.")
    if kind == "dem":
        files += [p for suffix in (".aux.xml", ".ovr", ".msk")
                  if (p := Path(str(path) + suffix)).is_file()]
    return [{"path": str(p), "bytes": p.stat().st_size, "sha256": sha256(p)} for p in files]


def run(config_path):
    with config_path.open(encoding="utf-8") as stream:
        cfg = json.load(stream)
    require(isinstance(cfg, dict), "The configuration must be a JSON object.")
    base = config_path.parent
    outdir = validate_config(cfg, base)

    # These imports occur only on an explicitly authorized invocation.
    # This is a process-local guard; no saved environment configuration changes.
    os.environ["PROJ_NETWORK"] = "OFF"
    import numpy as np
    import pandas as pd
    import geopandas as gpd
    import pyproj
    from pyproj import CRS, Geod, Transformer, network
    from shapely.geometry import LineString, box
    from shapely.ops import transform as geometry_transform, unary_union
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, ft2font
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import cartopy.crs as ccrs

    network.set_network_enabled(False)
    require(tuple(int(x) for x in pyproj.__version__.split(".")[:2]) >= (3, 5),
            "pyproj >= 3.5 is required for strict local transformation selection.")
    require(tuple(int(x) for x in pyproj.proj_version_str.split(".")[:2]) >= (9, 2),
            "PROJ >= 9.2 is required. Do not install automatically.")
    report = {"schema_version": "1.0", "quality_status": "needs_visual_review",
              "created_at": datetime.now(timezone.utc).isoformat(),
              "config_path": str(config_path), "config_sha256": sha256(config_path),
              "script_path": str(Path(__file__).resolve()), "script_sha256": sha256(Path(__file__)),
              "config": cfg, "inputs": {}, "panels": {}, "transformations": [], "warnings": [],
              "checks": {}, "outputs": [], "python": sys.version, "library_versions": {},
              "limitations": ["Human review of geographic meaning, boundary expression, labels and licensing is required.",
                              "No historical-boundary reconstruction, geocoding, data download or GCJ-02/BD-09 conversion.",
                              "No dateline/polar layouts, 3D/vertical datum conversion or automatic label placement."]}
    for package in ("geopandas", "cartopy", "matplotlib", "numpy", "pandas", "pyproj", "shapely",
                    "pyogrio", "fiona", "openpyxl", "rasterio"):
        try:
            report["library_versions"][package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            report["library_versions"][package] = None
    report["library_versions"]["PROJ"] = pyproj.proj_version_str

    def note(message):
        if message not in report["warnings"]:
            report["warnings"].append(message)

    for index, panel in enumerate(cfg["panels"]):
        left, bottom, width, height = panel["rect"]
        for other in cfg["panels"][index + 1:]:
            ox, oy, ow, oh = other["rect"]
            if min(left + width, ox + ow) > max(left, ox) and min(bottom + height, oy + oh) > max(bottom, oy):
                note(f"Panels {panel['id']} and {other['id']} overlap; inspect inset visibility and all annotations.")
    note("Source attribution is printed exactly as supplied in caption; confirm that it satisfies every dataset's terms.")

    transformers = {}

    def transformer(source, target):
        source, target = CRS(source), CRS(target)
        key = (source.to_wkt(), target.to_wkt())
        if key not in transformers:
            transformers[key] = Transformer.from_crs(source, target, always_xy=True,
                                                     allow_ballpark=False, only_best=True)
        return transformers[key]

    def transform_geometries(frame, target):
        target = CRS(target)
        if CRS(frame.crs).equals(target, ignore_axis_order=True):
            return frame.copy()
        operation = transformer(frame.crs, target)
        geometries = [geometry_transform(partial(operation.transform, errcheck=True), geom)
                      for geom in frame.geometry]
        result = frame.drop(columns=frame.geometry.name).copy()
        result = gpd.GeoDataFrame(result, geometry=geometries, crs=target, index=frame.index)
        require(np.isfinite(result.total_bounds).all() and result.is_valid.all(),
                "Reprojection produced invalid geometry; choose an appropriate projection/data extent.")
        return result

    def verify_crs(layer, actual):
        declared = layer.get("source_crs")
        if actual is None:
            require(declared, f"{layer['id']}: CRS metadata is missing; specify a verified source_crs.")
            actual = CRS(declared)
            note(f"{layer['id']}: missing source metadata supplied by explicit source_crs; verify its evidence.")
        else:
            actual = CRS(actual)
            if declared:
                require(actual.equals(CRS(declared), ignore_axis_order=True),
                        f"{layer['id']}: source_crs conflicts with metadata; do not relabel coordinates.")
        declared_system = layer["coordinate_system"]
        geodetic = actual.geodetic_crs
        require(geodetic is not None, f"{layer['id']}: a horizontal geographic/projected CRS is required.")
        geographic_code = geodetic.to_epsg()
        if declared_system == "WGS84":
            require(geographic_code in {4326, 4979, 4978},
                    f"{layer['id']}: declared WGS84 does not agree with CRS datum metadata.")
        if declared_system == "CGCS2000":
            require(geographic_code in {4490}, f"{layer['id']}: declared CGCS2000 does not match CRS metadata.")
        require(actual.is_geographic or actual.is_projected, f"{layer['id']}: unsupported CRS type.")
        return actual

    layers = {layer["id"]: layer for layer in cfg["layers"]}
    frames = {}
    paths = {}
    for ident, layer in layers.items():
        path = local_path(layer["path"], base)
        paths[ident] = path
        manifest = input_manifest(path, layer["kind"], bool(layer.get("source_crs")))
        report["inputs"][ident] = {"files": manifest, "provenance": layer["provenance"]}
        for key, value in layer["provenance"].items():
            if value.strip().lower() in {"unknown", "unverified", "未知", "待核实"}:
                note(f"{ident}: provenance.{key} is unresolved; publication review is incomplete.")
        if layer["kind"] == "dem":
            require(path.suffix.lower() in {".tif", ".tiff"}, f"{ident}: DEM must be a local GeoTIFF, not a VRT.")
            with path.open("rb") as stream:
                require(stream.read(4) in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"},
                        f"{ident}: input does not have a TIFF signature; external reference formats are rejected.")
            import rasterio
            with rasterio.Env(PROJ_NETWORK="OFF"), rasterio.open(path, driver="GTiff") as dataset:
                require(dataset.driver == "GTiff", f"{ident}: expected a GTiff driver, not a virtual dataset.")
                crs = verify_crs(layer, dataset.crs)
                require(dataset.count == 1, f"{ident}: supply a single-band elevation GeoTIFF.")
                report["inputs"][ident].update({"crs_wkt": crs.to_wkt(), "shape": list(dataset.shape),
                                                 "elevation_unit": layer["elevation_unit"],
                                                 "vertical_datum": "not transformed; verify source provenance"})
                if dataset.units[0]:
                    unit_aliases = {"m": {"m", "metre", "meter", "metres", "meters"},
                                    "ft": {"ft", "foot", "feet"}}
                    require(dataset.units[0].lower() in unit_aliases[layer["elevation_unit"]],
                            f"{ident}: declared elevation unit conflicts with raster metadata.")
            continue
        if layer["kind"] == "points_table":
            require(layer.get("source_crs"), f"{ident}: coordinate tables require an explicit verified source_crs.")
            if path.suffix.lower() == ".csv":
                table = pd.read_csv(path)
            else:
                require(path.suffix.lower() in {".xlsx", ".xls"}, f"{ident}: use CSV/XLSX/XLS.")
                sheet = layer.get("sheet_name", 0)
                require(isinstance(sheet, (int, str)), f"{ident}: sheet_name must select one sheet.")
                table = pd.read_excel(path, sheet_name=sheet)
            xname, yname = layer.get("x"), layer.get("y")
            require(xname in table.columns and yname in table.columns and xname != yname,
                    f"{ident}: x/y must name distinct coordinate columns.")
            x, y = pd.to_numeric(table[xname], errors="coerce"), pd.to_numeric(table[yname], errors="coerce")
            require(np.isfinite(x).all() and np.isfinite(y).all(), f"{ident}: missing/non-numeric coordinates.")
            crs = verify_crs(layer, layer["source_crs"])
            frame = gpd.GeoDataFrame(table, geometry=gpd.points_from_xy(x, y), crs=crs)
        else:
            suffix = path.suffix.lower()
            require(suffix in {".gpkg", ".geojson", ".json", ".shp", ".zip"}, f"{ident}: unsupported vector format.")
            resource = str(path)
            if suffix in {".gpkg", ".shp"}:
                with path.open("rb") as stream:
                    header = stream.read(16)
                require(header.startswith(b"SQLite format 3\x00") if suffix == ".gpkg"
                        else header.startswith(b"\x00\x00\x27\x0a"), f"{ident}: file format signature does not match its extension.")
            if suffix == ".gpkg":
                require(isinstance(layer.get("layer"), str) and layer["layer"].strip(),
                        f"{ident}: GeoPackage requires an explicit layer name; never assume the first layer.")
            if suffix == ".zip":
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    require(all(not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts
                                and "\\" not in n and ":" not in n for n in names), "Unsafe ZIP member path.")
                    member = shapefile_members(names, has_verified_crs=bool(layer.get("source_crs")))
                    with archive.open(member) as shape_stream:
                        require(shape_stream.read(4) == b"\x00\x00\x27\x0a", f"{ident}: ZIP .shp member has an invalid signature.")
                resource = f"/vsizip/{path}/{member}"
            if suffix in {".geojson", ".json"}:
                with path.open(encoding="utf-8") as stream:
                    geojson = json.load(stream)
                require(isinstance(geojson, dict) and geojson.get("type") in {"FeatureCollection", "Feature"},
                        f"{ident}: expected GeoJSON FeatureCollection/Feature.")
                if "crs" not in geojson:
                    require(layer["coordinate_system"] == "WGS84", f"{ident}: RFC 7946 GeoJSON is WGS84.")
                    if layer.get("source_crs"):
                        require(CRS(layer["source_crs"]).equals(CRS(4326), ignore_axis_order=True),
                                f"{ident}: RFC 7946 GeoJSON conflicts with source_crs.")
                    note(f"{ident}: GeoJSON without a legacy crs member interpreted as RFC 7946 WGS84 longitude/latitude.")
                else:
                    legacy = geojson["crs"]
                    require(isinstance(legacy, dict) and legacy.get("type") == "name"
                            and isinstance(legacy.get("properties", {}).get("name"), str),
                            f"{ident}: only an explicit named legacy GeoJSON CRS is accepted; linked CRS definitions are rejected.")
            kwargs = {}
            if layer.get("layer") is not None:
                require(suffix == ".gpkg", f"{ident}: layer selection applies only to GeoPackage.")
                kwargs["layer"] = layer["layer"]
            frame = gpd.read_file(resource, **kwargs)
            crs = verify_crs(layer, frame.crs)
            if frame.crs is None:
                frame = frame.set_crs(crs)
        require(len(frame) > 0 and frame.geometry.notna().all() and not frame.geometry.is_empty.any(),
                f"{ident}: empty/null geometry; repair a copy explicitly before rendering.")
        require(frame.geometry.is_valid.all(), f"{ident}: invalid geometry; automatic repair is disabled.")
        require(not frame.geometry.has_z.any(), f"{ident}: 3D vector geometries require a reviewed 2D conversion.")
        allowed = {"Point", "MultiPoint", "Polygon", "MultiPolygon", "LineString", "MultiLineString"}
        require(set(frame.geometry.geom_type) <= allowed, f"{ident}: unsupported mixed/collection geometry.")
        require(np.isfinite(frame.total_bounds).all(), f"{ident}: non-finite coordinates.")
        geographic = transform_geometries(frame, "EPSG:4326")
        west, south, east, north = geographic.total_bounds
        require(-180 <= west <= east <= 180 and -90 <= south <= north <= 90,
                f"{ident}: transformed coordinates are outside geographic ranges.")
        frames[ident] = frame
        report["inputs"][ident].update({"feature_count": len(frame), "crs_wkt": crs.to_wkt(),
                                         "bounds_wgs84": [float(v) for v in geographic.total_bounds]})
        if layer.get("label_field"):
            require(layer["label_field"] in frame.columns, f"{ident}: label_field does not exist.")

    boundary_id = cfg["study_area"]["boundary_layer"]
    require(boundary_id in frames, "Study boundary must be a vector polygon layer.")
    boundary = frames[boundary_id]
    require(set(boundary.geometry.geom_type) <= {"Polygon", "MultiPolygon"}, "Study boundary must be polygonal.")
    require(layers[boundary_id]["role"] == "study_area", "Boundary layer role must be study_area.")
    expected = cfg["study_area"].get("expected_bbox_wgs84")
    if expected:
        geographical = transform_geometries(boundary, 4326)
        require(all(box(*expected).covers(g) for g in geographical.geometry),
                "Study boundary exceeds expected_bbox_wgs84; verify the data/region before plotting.")
    boundary_union = unary_union(list(boundary.geometry))
    sample_counts = {}
    for ident, frame in frames.items():
        if layers[ident]["role"] != "samples":
            continue
        require(set(frame.geometry.geom_type) <= {"Point", "MultiPoint"}, f"{ident}: samples must be points.")
        aligned = transform_geometries(frame, boundary.crs)
        outside = [str(index) for index, geom in aligned.geometry.items() if not boundary_union.covers(geom)]
        sample_counts[ident] = {"outside_feature_count": len(outside), "first_20_row_indices": outside[:20]}
        if outside:
            note(f"{ident}: {len(outside)} sample features fall outside the study boundary; none were silently removed.")
    report["checks"]["sample_boundary"] = sample_counts

    layout = cfg["layout"]
    font = font_manager.FontProperties(family=layout["font_family"])
    try:
        font_path = font_manager.findfont(font, fallback_to_default=False)
    except ValueError as error:
        raise MapInputError(f"Requested font is not installed: {layout['font_family']}; select an available font.") from error
    report["font"] = {"family": layout["font_family"], "path": font_path, "sha256": sha256(Path(font_path))}
    plt.rcParams.update({"font.family": layout["font_family"], "font.size": layout["font_size_pt"],
                         "axes.unicode_minus": False, "pdf.fonttype": 42, "ps.fonttype": 42,
                         "svg.fonttype": "path", "text.usetex": False})
    fig = plt.figure(figsize=(layout["width_mm"] / 25.4, layout["height_mm"] / 25.4), dpi=layout["dpi"])
    axes, panel_crs, projected_frames, geographic_frames = {}, {}, {}, {}
    labels = []
    visible_study_panels = 0
    rendered_roles = set()
    role_names = {"zh": {"context": "区域背景", "study_area": "研究区", "roads": "道路", "water": "水系",
                          "samples": "样点", "terrain": "地形"},
                  "en": {"context": "Regional context", "study_area": "Study area", "roads": "Roads",
                          "water": "Water", "samples": "Samples", "terrain": "Terrain"}}
    defaults = {"context": {"facecolor": "#f4f2ed", "edgecolor": "#898989", "linewidth": 0.4},
                "study_area": {"facecolor": "none", "edgecolor": "#a3272b", "linewidth": 1.3},
                "roads": {"color": "#9b754a", "linewidth": 0.6},
                "water": {"facecolor": "#bdd9e9", "color": "#568fac", "edgecolor": "#568fac", "linewidth": 0.6},
                "samples": {"color": "#8b2040", "edgecolor": "white", "linewidth": 0.4, "markersize": 16},
                "terrain": {"facecolor": "#dad8cf", "edgecolor": "#aaa89f", "linewidth": 0.3}}

    for panel in cfg["panels"]:
        ident = panel["id"]
        crs = CRS(panel["crs"])
        require(crs.is_projected and crs.to_epsg() != 3857, f"{ident}: use a suitable projected EPSG other than 3857.")
        require(crs.area_of_use is not None, f"{ident}: EPSG area of use unavailable.")
        area = crs.area_of_use
        w, s, e, n = panel["extent_wgs84"]
        require(area.west <= w < e <= area.east and area.south <= s < n <= area.north,
                f"{ident}: requested extent exceeds EPSG area of use; choose another projection.")
        projection = ccrs.epsg(crs.to_epsg())
        ax = fig.add_axes(panel["rect"], projection=projection)
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
        ax.set_autoscale_on(False)
        ax.set_title(panel.get("title", ""), fontsize=layout["font_size_pt"], pad=4)
        ax.set_facecolor("white")
        axes[ident], panel_crs[ident] = ax, crs
        x0, x1, y0, y1 = ax.get_extent(crs=projection)
        require(all(math.isfinite(v) for v in (x0, x1, y0, y1)) and x0 < x1 and y0 < y1,
                f"{ident}: unusable projected extent.")
        report["panels"][ident] = {"crs_wkt": crs.to_wkt(), "requested_extent_wgs84": panel["extent_wgs84"],
                                    "actual_projected_extent": [x0, x1, y0, y1], "layers": {}}
        viewport_edge = ([(x, y0) for x in np.linspace(x0, x1, 65)]
                         + [(x1, y) for y in np.linspace(y0, y1, 65)[1:]]
                         + [(x, y1) for x in np.linspace(x1, x0, 65)[1:]]
                         + [(x0, y) for y in np.linspace(y1, y0, 65)[1:]])
        inverse = transformer(crs, 4326)
        viewport_lonlat = [list(inverse.transform(x, y, errcheck=True)) for x, y in viewport_edge]
        lon_values, lat_values = zip(*viewport_lonlat)
        require(all(math.isfinite(v) for xy in viewport_lonlat for v in xy)
                and max(lon_values) - min(lon_values) < 180,
                f"{ident}: viewport crosses a projection seam/dateline; use a dedicated workflow.")
        geographic_window = box(min(lon_values), min(lat_values), max(lon_values), max(lat_values))
        report["panels"][ident]["viewport_boundary_wgs84"] = viewport_lonlat
        dem_ids = [i for i in panel["layers"] if layers[i]["kind"] == "dem"]
        require(len(dem_ids) <= 1, f"{ident}: use at most one DEM per panel.")
        handles, handle_labels = [], set()
        visible_layers = 0
        # Render terrain first so it cannot conceal vector boundaries and samples.
        drawing_order = dem_ids + [i for i in panel["layers"] if i not in dem_ids]
        for order, layer_id in enumerate(drawing_order):
            layer = layers[layer_id]
            role = layer["role"]
            style = dict(defaults[role])
            style.update(layer.get("style", {}))
            if style.get("alpha", 1) == 0:
                note(f"{ident}/{layer_id}: alpha is zero, so this layer will be invisible.")
            label = layer.get("label") or role_names[cfg["language"]][role]
            if layer["kind"] == "dem":
                import rasterio
                from rasterio.warp import reproject, Resampling
                from rasterio.transform import from_bounds
                nx = min(2200, max(64, int(layout["width_mm"] / 25.4 * layout["dpi"] * panel["rect"][2])))
                ny = min(2200, max(64, int(layout["height_mm"] / 25.4 * layout["dpi"] * panel["rect"][3])))
                data = np.full((ny, nx), np.nan, dtype="float32")
                with rasterio.Env(PROJ_NETWORK="OFF"), rasterio.open(paths[layer_id], driver="GTiff") as src:
                    source = verify_crs(layer, src.crs)
                    operation = transformer(source, crs)
                    operation.transform((src.bounds.left + src.bounds.right) / 2,
                                        (src.bounds.bottom + src.bounds.top) / 2, errcheck=True)
                    definition = operation.definition
                    if definition == "unavailable until proj_trans is called":
                        definition = operation.get_last_used_operation().definition
                    reproject(source=rasterio.band(src, 1), destination=data,
                              src_transform=src.transform, src_crs=source.to_wkt(), src_nodata=src.nodata,
                              dst_transform=from_bounds(x0, y0, x1, y1, nx, ny), dst_crs=crs.to_wkt(),
                              dst_nodata=np.nan, resampling=Resampling.bilinear,
                              COORDINATE_OPERATION=definition)
                    data = data * src.scales[0] + src.offsets[0]
                valid = np.isfinite(data)
                require(valid.any(), f"{ident}/{layer_id}: no DEM coverage within the panel.")
                visible_layers += 1
                if style.get("alpha", 1) > 0:
                    rendered_roles.add(role)
                if not valid.all():
                    note(f"{ident}/{layer_id}: DEM has nodata or incomplete coverage; no gaps were fabricated.")
                artist = ax.imshow(np.ma.masked_invalid(data), extent=(x0, x1, y0, y1),
                                   origin="upper", transform=projection, cmap="terrain", zorder=0,
                                   alpha=style.get("alpha", 1), interpolation="nearest")
                color_ax = ax.inset_axes([0.935, 0.14, 0.025, 0.3])
                colorbar = fig.colorbar(artist, cax=color_ax)
                colorbar.set_label(("高程" if cfg["language"] == "zh" else "Elevation") + f" ({layer['elevation_unit']})",
                                   fontsize=layout["font_size_pt"] - 1)
                colorbar.ax.tick_params(labelsize=layout["font_size_pt"] - 1)
                report["panels"][ident]["layers"][layer_id] = {"valid_raster_fraction": float(valid.mean()),
                                                               "render_grid_shape": [ny, nx],
                                                               "resampling": "bilinear"}
                continue
            cache_key = (layer_id, ident)
            if cache_key not in projected_frames:
                if layer_id not in geographic_frames:
                    geographic_frames[layer_id] = transform_geometries(frames[layer_id], 4326)
                geographical = geographic_frames[layer_id]
                local_features = geographical[geographical.geometry.intersects(geographic_window)].copy()
                if local_features.empty:
                    note(f"{ident}/{layer_id}: no features intersect the geographic viewport envelope.")
                    report["panels"][ident]["layers"][layer_id] = {"visible_features": 0}
                    continue
                # Clip before local projection so global background data do not
                # cross the local projection's singularity or invalid domain.
                local_features.geometry = local_features.geometry.intersection(geographic_window)
                local_features = local_features[~local_features.geometry.is_empty]
                projected_frames[cache_key] = transform_geometries(local_features, crs)
            projected = projected_frames[cache_key]
            viewport = box(x0, y0, x1, y1)
            visible = projected[projected.geometry.intersects(viewport)].copy()
            report["panels"][ident]["layers"][layer_id] = {"visible_features": len(visible)}
            if visible.empty:
                note(f"{ident}/{layer_id}: no features intersect the panel.")
                continue
            visible.geometry = visible.geometry.intersection(viewport)
            linewidth, alpha = style.get("linewidth", 0.6), style.get("alpha", 1)
            zorder = 2 + order
            kind_types = set(visible.geometry.geom_type)
            polygons = visible[visible.geom_type.isin(["Polygon", "MultiPolygon"])]
            lines = visible[visible.geom_type.isin(["LineString", "MultiLineString"])]
            points = visible[visible.geom_type.isin(["Point", "MultiPoint"])].explode(index_parts=False)
            handle = None
            if not polygons.empty:
                face = style.get("facecolor", style.get("color", "none"))
                edge = style.get("edgecolor", style.get("color", "black"))
                ax.add_geometries(polygons.geometry, projection, facecolor=face, edgecolor=edge,
                                  linewidth=linewidth, alpha=alpha, zorder=zorder)
                handle = Patch(facecolor=face, edgecolor=edge, linewidth=linewidth, alpha=alpha, label=label)
            if not lines.empty:
                color = style.get("color", style.get("edgecolor", "black"))
                ax.add_geometries(lines.geometry, projection, facecolor="none", edgecolor=color,
                                  linewidth=linewidth, alpha=alpha, zorder=zorder)
                handle = Line2D([], [], color=color, linewidth=linewidth, alpha=alpha, label=label)
            if not points.empty:
                color = style.get("color", style.get("facecolor", "black"))
                size = style.get("markersize", 16)
                ax.scatter(points.geometry.x, points.geometry.y, transform=projection, s=size, c=color,
                           edgecolors=style.get("edgecolor", "white"), linewidths=linewidth,
                           alpha=alpha, zorder=zorder)
                handle = Line2D([], [], marker="o", linestyle="", markerfacecolor=color,
                                markeredgecolor=style.get("edgecolor", "white"), markersize=math.sqrt(size),
                                alpha=alpha, label=label)
            unsupported = kind_types - {"Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point", "MultiPoint"}
            if unsupported:
                note(f"{ident}/{layer_id}: clipped geometry collections are omitted; inspect edge features.")
            if handle is not None:
                visible_layers += 1
                if alpha > 0:
                    rendered_roles.add(role)
                if layer_id == boundary_id and any(g.area > 0 for g in polygons.geometry):
                    visible_study_panels += 1
            if handle is not None and label not in handle_labels:
                handles.append(handle)
                handle_labels.add(label)
            elif handle is not None:
                note(f"{ident}: more than one visible layer uses legend label '{label}'; assign distinct layer.label values if semantics/styles differ.")
            if layer.get("label_field"):
                require(len(visible) <= 500, f"{ident}/{layer_id}: over 500 labels; preselect a label layer.")
                for _, feature in visible.iterrows():
                    text = feature[layer["label_field"]]
                    if pd.isna(text):
                        continue
                    point = feature.geometry.representative_point()
                    artist = ax.annotate(str(text), (point.x, point.y), xytext=(2, 2), textcoords="offset points",
                                         fontsize=layout["font_size_pt"], zorder=100, clip_on=True)
                    labels.append((ident, artist))
        if handles:
            ax.legend(handles=handles, loc="upper left", fontsize=layout["font_size_pt"] - 1,
                      framealpha=0.9, borderpad=0.4, handlelength=1.4)
        require(visible_layers > 0, f"{ident}: panel has no visible spatial data; verify extent and layers.")

    require(visible_study_panels > 0, "No panel shows a nonzero-area study boundary; check the study area and extents.")
    required_roles = {"administrative": {"study_area"}, "terrain_hydrology": {"study_area", "terrain", "water"},
                      "transport_samples": {"study_area", "roads", "samples"}}[cfg["map_type"]]
    missing_roles = sorted(required_roles - rendered_roles)
    report["checks"]["map_type_missing_visible_roles"] = missing_roles
    if missing_roles:
        note(f"Map type {cfg['map_type']} has no visible data for roles {', '.join(missing_roles)}; the intended figure is incomplete.")

    geod = Geod(ellps="WGS84")

    def horizontal_distance(inv, start, end, y):
        xs = np.linspace(start, end, 33)
        lons, lats = inv.transform(xs, np.full(33, y), errcheck=True)
        return abs(geod.line_length(lons, lats))

    for panel in cfg["panels"]:
        ident = panel["id"]
        ax, crs = axes[ident], panel_crs[ident]
        x0, x1, y0, y1 = ax.get_extent(crs=ax.projection)
        inv = transformer(crs, 4326)
        forward = transformer(4326, crs)
        if panel.get("scale_bar_km"):
            start, y = x0 + 0.08 * (x1 - x0), y0 + 0.09 * (y1 - y0)
            target = panel["scale_bar_km"] * 1000
            maximum = start + 0.4 * (x1 - x0)
            require(horizontal_distance(inv, start, maximum, y) >= target,
                    f"{ident}: scale bar is too long; choose a shorter scale_bar_km.")
            positions = [start]
            for fraction in (0.5, 1):
                low, high = start, maximum
                for _ in range(48):
                    middle = (low + high) / 2
                    if horizontal_distance(inv, start, middle, y) < fraction * target:
                        low = middle
                    else:
                        high = middle
                positions.append((low + high) / 2)
            tick = (y1 - y0) * 0.012
            ax.plot(positions, [y] * 3, color="black", linewidth=1.4, zorder=150)
            for i, x in enumerate(positions):
                ax.plot([x, x], [y - tick / 2, y + tick / 2], color="black", linewidth=0.8, zorder=150)
                artist = ax.text(x, y - tick, f"{panel['scale_bar_km'] * i / 2:g}" + (" km" if i == 2 else ""),
                                 ha="center", va="top", fontsize=layout["font_size_pt"] - 1, zorder=150,
                                 bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75, "pad": 0.5})
                labels.append((ident, artist))
            variations = [horizontal_distance(inv, start, positions[-1], y0 + f * (y1 - y0)) / target - 1
                          for f in (0.1, 0.5, 0.9)]
            report["panels"][ident]["scale_bar"] = {"label_km": panel["scale_bar_km"],
                "measured_ground_m": horizontal_distance(inv, start, positions[-1], y),
                "position_projected": [start, y], "same_projected_length_relative_variation": variations,
                "meaning": "Ground distance along this horizontal bar at its own location; not a global scale."}
            if max(abs(v) for v in variations) > 0.05:
                note(f"{ident}: map scale varies by over 5% along sampled rows; consider removing the overview scale bar.")
        if panel.get("north_arrow"):
            anchor = (x0 + 0.88 * (x1 - x0), y0 + 0.75 * (y1 - y0))
            lon, lat = inv.transform(*anchor, errcheck=True)
            lon_n, lat_n, _ = geod.fwd(lon, lat, 0, 1000)
            north_x, north_y = forward.transform(lon_n, lat_n, errcheck=True)
            dx, dy = (north_x - anchor[0]) / (x1 - x0), (north_y - anchor[1]) / (y1 - y0)
            norm = math.hypot(dx, dy)
            require(norm > 0 and math.isfinite(norm), f"{ident}: cannot determine local true north.")
            tip = (0.88 + 0.12 * dx / norm, 0.75 + 0.12 * dy / norm)
            ax.annotate("", xy=tip, xytext=(0.88, 0.75), xycoords="axes fraction",
                        arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1}, zorder=150)
            artist = ax.text(*tip, "N", transform=ax.transAxes, ha="center", va="bottom", zorder=150)
            labels.append((ident, artist))
            report["panels"][ident]["north_arrow"] = {"type": "local true north", "anchor_wgs84": [lon, lat]}

    # Sample each final projected viewport edge, then transform that boundary to
    # the locator panel. Transforming just two geographic corners is incorrect.
    fig.canvas.draw()
    for panel in cfg["panels"]:
        ax = axes[panel["id"]]
        for target_id in panel.get("locator_for", []):
            target_ax = axes[target_id]
            x0, x1, y0, y1 = target_ax.get_extent(crs=target_ax.projection)
            coordinates = ([(x, y0) for x in np.linspace(x0, x1, 65)]
                           + [(x1, y) for y in np.linspace(y0, y1, 65)[1:]]
                           + [(x, y1) for x in np.linspace(x1, x0, 65)[1:]]
                           + [(x0, y) for y in np.linspace(y1, y0, 65)[1:]])
            operation = transformer(panel_crs[target_id], panel_crs[panel["id"]])
            ring = geometry_transform(partial(operation.transform, errcheck=True), LineString(coordinates))
            require(np.isfinite(ring.bounds).all(), f"Locator {target_id} cannot be transformed into {panel['id']}.")
            ax.add_geometries([ring], ax.projection, facecolor="none", edgecolor="#a3272b", linewidth=0.9,
                              linestyle="--", zorder=140)
            report["panels"][panel["id"]].setdefault("locators", []).append({"target": target_id,
                "target_actual_extent": [x0, x1, y0, y1], "edge_samples": len(coordinates)})
            inv = transformer(panel_crs[target_id], 4326)
            lonlat = [list(inv.transform(x, y, errcheck=True)) for x, y in coordinates]
            report["panels"][target_id]["viewport_boundary_wgs84"] = lonlat
            outer_extent = ax.get_extent(crs=ax.projection)
            if not box(outer_extent[0], outer_extent[2], outer_extent[1], outer_extent[3]).covers(ring):
                note(f"{panel['id']}: locator for {target_id} is partially outside the overview panel.")

    if cfg.get("caption"):
        fig.text(0.02, 0.012, cfg["caption"], ha="left", va="bottom", fontsize=layout["font_size_pt"] - 1)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    overlaps = []
    for i, (panel_id, artist) in enumerate(labels):
        extent = artist.get_window_extent(renderer)
        for other_id, other in labels[i + 1:]:
            if other_id == panel_id and extent.overlaps(other.get_window_extent(renderer)):
                overlaps.append({"panel": panel_id, "first": artist.get_text(), "second": other.get_text()})
    report["checks"]["possible_text_collisions"] = overlaps[:50]
    if overlaps:
        note(f"Detected {len(overlaps)} possible text overlaps; labels were not automatically moved.")
    note("Collision checks cover plotted labels/scale/north text only; review legends, inset colorbars, panel titles and captions visually.")
    text_artists = fig.findobj(matplotlib.text.Text)
    font_chars = ft2font.FT2Font(font_path).get_charmap()
    missing = sorted({char for artist in text_artists for char in artist.get_text()
                      if not char.isspace() and ord(char) not in font_chars})
    report["checks"]["missing_font_glyphs"] = missing
    require(not missing, f"Requested font lacks glyphs: {''.join(missing[:50])}; choose a font with complete coverage.")
    outside_figure = []
    for artist in text_artists:
        if artist.get_visible() and artist.get_text():
            corners = artist.get_window_extent(renderer).get_points()
            if not all(fig.bbox.contains(*corner) for corner in corners):
                outside_figure.append(artist.get_text())
    report["checks"]["text_outside_figure"] = outside_figure
    if outside_figure:
        note("Some text falls outside the fixed figure dimensions; adjust layout before publication.")
    for operation in transformers.values():
        report["transformations"].append({"source": operation.source_crs.to_string(),
            "target": operation.target_crs.to_string(), "description": operation.description,
            "pipeline": operation.definition, "accuracy_m": operation.accuracy,
            "network_enabled": operation.is_network_enabled})
        if operation.accuracy < 0:
            note("At least one coordinate operation reports unknown accuracy; verify suitability for the study scale.")
    # Final output is created only after checks; existing runs are never reused.
    outdir.mkdir(parents=True, exist_ok=False)
    incomplete = outdir / ".incomplete"
    incomplete.write_text("Rendering has not yet completed.\n", encoding="utf-8")
    write_json_new(outdir / "config.used.json", cfg)
    try:
        for fmt in cfg["outputs"]["formats"]:
            destination = outdir / f"{cfg['outputs']['basename']}.{fmt}"
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with destination.open("xb") as output:
                    fig.savefig(output, format=fmt, dpi=layout["dpi"], facecolor="white")
                for item in caught:
                    note(str(item.message))
            report["outputs"].append({"path": str(destination), "sha256": sha256(destination),
                                       "bytes": destination.stat().st_size})
        report["checks"]["data_alignment"] = "CRS transformed consistently; independent control-point review still required."
        write_json_new(outdir / "run-report.json", report)
        incomplete.unlink()
    except Exception as error:
        report["quality_status"] = "failed"
        report["error"] = str(error)
        write_json_new(outdir / "failure-report.json", report)
        raise
    finally:
        plt.close(fig)
    print(json.dumps({"output_directory": str(outdir), "quality_status": report["quality_status"],
                      "warnings": report["warnings"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="JSON configuration; all relative paths resolve beside this file.")
    args = parser.parse_args()
    try:
        config = local_path(args.config, Path.cwd())
        run(config)
    except (MapInputError, ImportError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Cannot render: {error}", file=sys.stderr)
        print("No data downloads or installations are requested by this program. Review configuration/environment before retrying.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
