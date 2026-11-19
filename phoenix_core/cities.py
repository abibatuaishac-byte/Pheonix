"""City registry.

One entry per city.  Everything city-specific lives here or in the city's data
folder; the analysis code and UI are shared.  A dataset is only treated as
available if its file exists on disk, so the status table can never claim data
that is not actually there.

To add a city: create ``data/cities/<slug>/``, run ``scripts/fetch_city_data.py``
with a new entry in ``scripts/city_sources.py``, then add a ``City`` here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FREETOWN_PROCESSED = DATA / "processed"


@dataclass
class Scenario:
    key: str
    label: str
    short: str
    kind: str              # "modelled" | "regulatory"
    file: Path
    annual_chance_pct: float | None
    description: str


@dataclass
class Dataset:
    name: str
    role: str              # hazard | infrastructure | exposure | boundary | observations | live
    source: str
    url: str
    license: str
    coverage: str
    date: str              # publication or retrieval date, as known
    limitations: str
    file: Path | None = None   # None = live API, never "missing"

    @property
    def available(self) -> bool:
        return self.file is None or self.file.exists()


@dataclass
class City:
    slug: str
    name: str
    country: str
    region: str
    center: tuple[float, float]
    zoom: int
    population_note: str
    context: str
    data_dir: Path
    scenarios: list[Scenario] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    hazard_caveat: str = ""
    nws_alerts: bool = False      # US National Weather Service alerts available
    study_area_note: str = ""
    river_point: tuple[float, float, str] | None = None   # GloFAS cell for discharge forecasts
    river_note: str = ""

    # ------------------------------------------------------------------ files
    @property
    def boundary_file(self) -> Path:
        return self.data_dir / "boundary.geojson"

    @property
    def facilities_file(self) -> Path:
        return self.data_dir / "facilities.geojson"

    @property
    def roads_file(self) -> Path:
        return self.data_dir / "roads.geojson"

    @property
    def manifest(self) -> dict:
        p = self.data_dir / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    @property
    def available_scenarios(self) -> list[Scenario]:
        return [s for s in self.scenarios if s.file.exists()]

    @property
    def has_flood_analysis(self) -> bool:
        return bool(self.available_scenarios) and self.facilities_file.exists()

    @property
    def analysis_status(self) -> str:
        if self.has_flood_analysis:
            return "Flood exposure analysis available"
        if self.available_scenarios:
            return "Hazard data only (infrastructure missing)"
        return "Profile only: no verified hazard data loaded"


def _osm_dataset(city_dir: Path, retrieved: str, coverage: str) -> Dataset:
    return Dataset(
        name="Critical facilities (hospitals, clinics, schools, emergency services)",
        role="infrastructure",
        source="OpenStreetMap contributors (extracts via HOT OSM Raw Data API or Overpass API)",
        url="https://www.openstreetmap.org/copyright",
        license="ODbL 1.0",
        coverage=coverage,
        date=f"Retrieved {retrieved}",
        limitations="Volunteered data: completeness varies by place and category. Names are shown only "
                    "where OSM has them; unnamed features are labelled as such, never invented.",
        file=city_dir / "facilities.geojson",
    )


def _roads_dataset(city_dir: Path, retrieved: str, coverage: str) -> Dataset:
    return Dataset(
        name="Major road network (motorway to secondary) and bridges",
        role="infrastructure",
        source="OpenStreetMap contributors (extracts via HOT OSM Raw Data API or Overpass API)",
        url="https://www.openstreetmap.org/copyright",
        license="ODbL 1.0",
        coverage=coverage,
        date=f"Retrieved {retrieved}",
        limitations="Road centre-lines only; elevation, drainage and actual passability are not represented.",
        file=city_dir / "roads.geojson",
    )


def _meteo_dataset() -> Dataset:
    return Dataset(
        name="Precipitation forecast and modelled river discharge (GloFAS)",
        role="live", source="Open-Meteo Forecast API and Flood API (Copernicus GloFAS)",
        url="https://open-meteo.com/", license="CC BY 4.0",
        coverage="Global grid; fetched live for the city centre", date="Live (fetched on demand)",
        limitations="Gridded model forecasts, not local observations. GloFAS discharge is for large rivers "
                    "on a ~5 km grid and does not represent drainage or flash flooding.",
        file=None)


def _fema_dataset(city_dir: Path, retrieved: str, coverage: str) -> Dataset:
    return Dataset(
        name="FEMA National Flood Hazard Layer (NFHL) - Flood Hazard Zones",
        role="hazard",
        source="Federal Emergency Management Agency (FEMA)",
        url="https://hazards.fema.gov/femaportal/wps/portal/NFHLWMS",
        license="US Government work (public domain)",
        coverage=coverage,
        date=f"Retrieved {retrieved} (effective dates vary by FIRM panel; see manifest)",
        limitations="Regulatory flood insurance zones, not a forecast. Represents the 1% and 0.2% annual-chance "
                    "riverine/coastal flood; does not map most rainfall-driven (pluvial) street flooding, "
                    "and some zones may be under revision.",
        file=city_dir / "fema_sfha.geojson",
    )


def _nws_dataset() -> Dataset:
    return Dataset(
        name="Active weather and flood alerts", role="live",
        source="US National Weather Service API (api.weather.gov)", url="https://www.weather.gov/documentation/services-web-api",
        license="US Government work (public domain)", coverage="Point query at the city centre",
        date="Live (fetched on demand)", limitations="Official alerts only; absence of an alert is not an all-clear.",
        file=None)


def _manifest_date(city_dir: Path, key: str, default: str = "unknown") -> str:
    p = city_dir / "manifest.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get("retrieved", {}).get(key, default)
    return default


def _us_city(slug, name, state, center, zoom, context, pop, study_note, river=None, river_note="") -> City:
    d = DATA / "cities" / slug
    osm_date = _manifest_date(d, "osm")
    fema_date = _manifest_date(d, "fema")
    cov = f"{study_note}"
    return City(
        slug=slug, name=name, country="United States", region=state, center=center, zoom=zoom,
        population_note=pop, context=context, data_dir=d, nws_alerts=True, study_area_note=study_note,
        river_point=river, river_note=river_note,
        scenarios=[
            Scenario("SFHA", "FEMA 1% annual-chance flood zone (Special Flood Hazard Area)", "1% annual chance",
                     "regulatory", d / "fema_sfha.geojson", 1.0,
                     "Zones A, AE, AH, AO, VE: the 'base flood' used for insurance and building regulation."),
            Scenario("X500", "FEMA 1% + 0.2% annual-chance flood zones", "0.2% annual chance",
                     "regulatory", d / "fema_500yr.geojson", 0.2,
                     "Adds shaded Zone X (0.2% annual chance) to the Special Flood Hazard Area."),
        ],
        datasets=[
            _fema_dataset(d, fema_date, cov),
            Dataset("City boundary", "boundary", "US Census Bureau TIGERweb (Incorporated Places)",
                    "https://tigerweb.geo.census.gov/", "US Government work (public domain)", name,
                    f"Retrieved {_manifest_date(d, 'boundary')}", "Generalised for display.", d / "boundary.geojson"),
            _osm_dataset(d, osm_date, cov),
            _roads_dataset(d, osm_date, cov),
            _meteo_dataset(),
            _nws_dataset(),
        ],
        missing=[
            "Building-level exposure (footprints with values) - not loaded; FEMA USA Structures could be added.",
            "Flood depth grids - NFHL zones give extent and (where present) base flood elevation, not depth at each asset.",
            "Pluvial / drainage flood modelling - not covered by the NFHL.",
            "Population exposure - not loaded.",
        ],
        hazard_caveat="FEMA zones are regulatory insurance maps of the 1% and 0.2% annual-chance flood. They are "
                      "historical/probabilistic hazard information, not a live forecast, and they under-represent "
                      "rainfall-driven street flooding.",
    )


def build_registry() -> dict[str, City]:
    ft_dir = DATA / "cities" / "freetown"
    wb_url = "https://datacatalog.worldbank.org/search/dataset/0066934/freetown-sierra-leone-flood-risk-assessment"
    freetown = City(
        slug="freetown", name="Freetown", country="Sierra Leone", region="Western Area Urban",
        center=(8.4657, -13.2317), zoom=13,
        population_note="Capital of Sierra Leone; dense informal settlements on steep slopes and coastal flats.",
        context="Freetown experiences intense rainy-season downpours (July-September) that drive river, "
                "surface-water and flash flooding, including the August 2017 Regent mudslide and flood disaster.",
        data_dir=ft_dir,
        study_area_note="Freetown City Council wards (World Bank assessment area)",
        river_note="Freetown's short, steep streams are below the ~5 km resolution of GloFAS, so river discharge "
                   "forecasts are not shown; use rainfall forecasts instead.",
        scenarios=[
            Scenario("RP20", "1-in-20-year flood (5% chance in any year)", "1-in-20 yr", "modelled",
                     FREETOWN_PROCESSED / "flood_extent_RP20.geojson", 5.0,
                     "JBA undefended combined river + surface-water flood extent, 20-year return period."),
            Scenario("RP100", "1-in-100-year flood (1% chance in any year)", "1-in-100 yr", "modelled",
                     FREETOWN_PROCESSED / "flood_extent_RP100.geojson", 1.0,
                     "JBA undefended combined river + surface-water flood extent, 100-year return period."),
        ],
        datasets=[
            Dataset("Probabilistic flood extents (RP20, RP100)", "hazard",
                    "World Bank - Freetown Flood Risk Assessment (hazard modelling by JBA Risk Management)",
                    wb_url, "CC BY 4.0", "Freetown city wards, 1 m resolution source; simplified (~4 m) for display",
                    "Published 2023; processed 2026-11-19",
                    "Modelled, undefended scenario; flood extent only (no depth in this download).",
                    FREETOWN_PROCESSED / "flood_extent_RP100.geojson"),
            Dataset("Modelled building exposure (153,436 buildings)", "exposure",
                    "World Bank - Freetown Flood Risk Assessment (GEM Foundation / JBA)", wb_url,
                    "CC BY 4.0", "Whole city", "Published 2023; processed 2026-11-19",
                    "Disaggregated modelled locations, not surveyed footprints; values are replacement-cost estimates.",
                    FREETOWN_PROCESSED / "exposed_buildings.csv"),
            Dataset("Ward boundaries and modelled average annual loss", "boundary",
                    "World Bank - Freetown Flood Risk Assessment", wb_url, "CC BY 4.0", "76 wards",
                    "Published 2023; processed 2026-11-19", "Ward AAL is from the source model, not recomputed.",
                    FREETOWN_PROCESSED / "wards.geojson"),
            Dataset("Recorded flash-flood sites and areas", "observations",
                    "Freetown City Council (distributed with the World Bank dataset)", wb_url, "CC BY 4.0",
                    "45 sites + mapped areas", "Undated records (published with 2023 dataset)",
                    "No event dates; locations may be approximate.",
                    FREETOWN_PROCESSED / "recorded_flood_points.geojson"),
            _osm_dataset(ft_dir, _manifest_date(ft_dir, "osm"), "Freetown wards bounding box"),
            _roads_dataset(ft_dir, _manifest_date(ft_dir, "osm"), "Freetown wards bounding box"),
            _meteo_dataset(),
        ],
        missing=[
            "Flood depth grids (not in the vector download used here).",
            "Return periods other than 1-in-20 and 1-in-100 (RP50, RP200, RP500, RP1500 exist as rasters in the source).",
            "Dated historical flood events.",
            "Official national alert feed (Sierra Leone NDMA) - no machine-readable API identified.",
        ],
        hazard_caveat="The World Bank / JBA extents are modelled probabilistic scenarios (undefended). They are not "
                      "a forecast or a record of a past flood.",
    )
    return {
        "freetown": freetown,
        "new_orleans": _us_city(
            "new_orleans", "New Orleans", "Louisiana", (29.9511, -90.0715), 12,
            "Much of the city lies at or below sea level, protected by the Hurricane and Storm Damage Risk "
            "Reduction System levees and pumped drainage.",
            "Major Gulf Coast city on the Mississippi River; Hurricane Katrina (2005) flooded about 80% of the city "
            "after levee failures.",
            "City of New Orleans / Orleans Parish (TIGERweb boundary)",
            river=(29.925, -90.175, "Mississippi River (GloFAS cell just upstream of the city)")),
        "houston": _us_city(
            "houston", "Houston", "Texas", (29.7604, -95.3698), 12,
            "Flat, low-lying city crossed by bayous; repeated major floods (Allison 2001, Harvey 2017).",
            "Fourth-largest US city, in Harris County; flooding is dominated by extreme rainfall on bayou watersheds.",
            "Central Houston study area (City of Houston clipped to 95.50-95.25 W, 29.68-29.84 N)",
            river=(29.725, -95.175, "Largest GloFAS channel near central Houston (29.725 N, 95.175 W)")),
        "miami": _us_city(
            "miami", "Miami", "Florida", (25.7617, -80.1918), 12,
            "Low-lying coastal city on porous limestone; exposed to storm surge, sea-level-rise 'sunny-day' "
            "tidal flooding and heavy rainfall.",
            "City of Miami in Miami-Dade County, on Biscayne Bay.",
            "City of Miami (TIGERweb boundary)",
            river_note="No GloFAS river cell represents Miami's canals and short rivers; discharge is not shown."),
    }


CITIES = build_registry()
