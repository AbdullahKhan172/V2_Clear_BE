"""
IFC Processor - Structural Quantity Extraction + 3D Geometry
============================================================
Full-featured IFC quantity extractor adapted from IFC_quantity.py V10.
Geometry-based calculations for accurate quantity estimation.

Supports:
- 5-level volume extraction cascade (Qto → Psets → Type → Geometry → Calculate)
- 21 geometry item types (ExtrudedArea, BRep, Boolean, FaceSets, etc.)
- 15 profile types (Rectangle, Circle, I-Shape, L-Shape, Hollow, etc.)
- 7 curve types (Polyline, IndexedPolyCurve, CompositeCurve, BSpline, etc.)
- Precast slab handling (hollowcore, waffle, ribbed void deductions)
- Steel/metal element detection and weight calculation
- Wall opening volume deduction
- Volume validation per element type
"""

import math
from collections import defaultdict

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as element_util
import ifcopenshell.util.unit as unit_util
import pandas as pd

from steel_sections import is_structural_steel_name


# ── Material densities (kg/m³) ─────────────────────────────────────────────
MATERIAL_DENSITIES = {
    'steel': 7850, 'stainless steel': 8000, 'iron': 7874,
    'aluminum': 2700, 'aluminium': 2700,
    'concrete': 2400, 'reinforced concrete': 2500,
    'lightweight concrete': 1800, 'precast concrete': 2400,
    'high strength concrete': 2500,
    'wood': 600, 'timber': 600,
    'brick': 1800, 'block': 2000, 'stone': 2500,
    'glass': 2500, 'gypsum': 1500,
    'default': 2400,
}

# ── Structural IFC types ───────────────────────────────────────────────────
STRUCTURAL_TYPES = [
    'IfcColumn', 'IfcBeam', 'IfcSlab', 'IfcWall',
    'IfcFooting', 'IfcPile', 'IfcMember', 'IfcPlate',
    'IfcRoof', 'IfcStair', 'IfcRamp',
    'IfcReinforcingBar', 'IfcReinforcingMesh', 'IfcTendon',
]

CATEGORY_MAP = {
    'IfcColumn': 'Column', 'IfcBeam': 'Beam',
    'IfcSlab': 'Slab/Floor', 'IfcWall': 'Wall', 'IfcWallStandardCase': 'Wall',
    'IfcFooting': 'Foundation/Footing', 'IfcPile': 'Pile',
    'IfcMember': 'Beam', 'IfcPlate': 'Slab/Floor',
    'IfcRoof': 'Roof', 'IfcStair': 'Stair',
    'IfcStairFlight': 'Stair', 'IfcRamp': 'Ramp', 'IfcRampFlight': 'Ramp',
    'IfcReinforcingBar': 'Reinforcement', 'IfcReinforcingMesh': 'Reinforcement',
    'IfcTendon': 'Post-Tensioning', 'IfcFoundation': 'Foundation/Footing',
}

# ── Non-structural element name/family keywords → override category ────────
GLAZING_KW = ['glaz', 'curtain', 'cladding', 'facade', 'panel', 'spandrel',
               'window', 'glazed', 'curtainwall', 'curtain wall', 'glass',
               'curtainwll', 'curtian', 'glazng', 'cladng', 'fasade', 'fascade']
DOOR_KW    = ['door', 'entrance', 'gate', 'dorr', 'enrance', 'entrnce']

# ── Structural sub-type keyword maps (checked in order — most specific first) ─
STRUCTURAL_OVERRIDES = [
    # Foundations — pile caps
    ('Pile Cap',          [
        'pile cap', 'pilecap', 'pile-cap', 'pile caps', 'pile-caps',
        'pile cap footing', 'cap pile', 'pile head', 'plie cap', 'pile capp',
        'pllecap', 'pile cp', 'p/cap', 'pcap',
    ]),
    # Foundations — pad / isolated
    # Checked BEFORE Raft Foundation: a Revit "isolated footing" element often
    # belongs to a family named "Foundation Slab" or similar, which would
    # otherwise match the Raft Foundation keyword 'foundation slab' first.
    ('Pad Footing',       [
        'pad footing', 'pad foundation', 'isolated footing', 'isolated foundation',
        'spread footing', 'pad fdn', 'pad fdtn', 'pad ftg', 'isolated fdn',
        'isol. footing', 'iso footing', 'col footing', 'column footing',
        'column pad', 'individual footing', 'spot footing', 'individual pad',
        'pad ftng', 'padd footing', 'padfdn', 'pad.footing',
    ]),
    # Foundations — strip
    # Also checked BEFORE Raft Foundation for the same reason.
    ('Strip Footing',     [
        'strip footing', 'strip foundation', 'continuous footing',
        'strip fdn', 'strip fdtn', 'strip ftg', 'wall footing', 'wall fdn',
        'linear footing', 'continous footing', 'continuos footing',
        'strip.footing', 'strp footing', 'cont. footing', 'cont footing',
    ]),
    # Foundations — raft / mat / generic foundation slab
    # Checked AFTER Pad and Strip so specific footing types win when both match.
    ('Raft Foundation',   [
        'raft foundation', 'raft slab', 'mat foundation', 'mat slab',
        'raft fndn', 'raft fnd', 'raft fdtn', 'mat fdn', 'raft footing',
        'raaft', 'rft foundation', 'rafft slab', 'mat. foundation',
        'floating slab', 'raft plate', 'rc raft', 'raft', 'matfoundation',
        # generic "foundation slab" naming (IfcSlab used as a base slab)
        'foundation slab', 'foundation floor', 'fndn slab', 'fdn slab',
        'base slab', 'base mat', 'basement base slab', 'foundation plate',
        'fnd slab', 'fdtn slab', 'ftn slab', 'foundationslab',
        'foundtion slab', 'foundaion slab', 'founation slab',
    ]),
    # Foundations — piles
    ('Pile',              [
        'pile', 'bored pile', 'driven pile', 'cfa pile', 'micropile', 'caisson',
        'bored piles', 'cast in-situ pile', 'cast insitu pile', 'cast in situ pile',
        'auger pile', 'auger cast pile', 'helical pile', 'screw pile',
        'friction pile', 'end bearing pile', 'sheet pile', 'secant pile',
        'contiguous pile', 'kingpost pile', 'h-pile', 'steel pile', 'spun pile',
        'precast pile', 'concrete pile', 'timber pile', 'micro pile',
        'plle', 'pille', 'caisson pile', 'deep foundation', 'drilled pier',
        'drilled shaft', 'pier', 'mini pile', 'minipile',
    ]),

    # Walls — core
    ('Core Wall',         [
        'core wall', 'lift core', 'stair core', 'elevator core', 'lift shaft wall',
        'core-wall', 'corewall', 'staircore', 'liftcore', 'service core',
        'rc core', 'concrete core', 'structural core', 'corewll', 'cor wall',
        'lift/stair core', 'lifft core', 'elevtor core',
    ]),
    # Walls — shear
    ('Shear Wall',        [
        'shear wall', 'shear-wall', 'shearwall', 'sw', 's/wall',
        'lateral wall', 'shear wll', 'shear wal', 'shear walls',
        'sheear wall', 'shr wall', 'sh wall', 'shearwl',
    ]),
    # Walls — retaining
    ('Retaining Wall',    [
        'retaining wall', 'retaining-wall', 'ret wall', 'ret. wall',
        'retaining wl', 'r/wall', 'r.wall', 'retaining walls',
        'retain wall', 'retaining-wl', 'rwall', 'retanining wall',
        'retaining wal', 'retianing wall', 'retianing wll',
    ]),
    # Walls — basement
    ('Basement Wall',     [
        'basement wall', 'basement-wall', 'below grade wall', 'b/wall',
        'bsmt wall', 'bsmnt wall', 'basement wl', 'b.wall',
        'substructure wall', 'below ground wall', 'blw grade wall',
        'below-grade wall', 'basement wll', 'basemnt wall',
    ]),

    # Beams — transfer
    ('Transfer Beam',     [
        'transfer beam', 'transfer girder', 'transfer-beam', 'xfer beam',
        'trans beam', 'trans. beam', 't.beam', 'transfer bm',
        'transver beam', 'transferr beam', 'tranfer beam',
        'trnsfr beam', 'transfer beaam',
    ]),
    # Beams — ground / grade
    ('Ground Beam',       [
        'ground beam', 'grade beam', 'ground-beam', 'grade-beam',
        'grd beam', 'gr beam', 'g/beam', 'gb', 'ground bm',
        'ground beams', 'grade bm', 'gr. beam', 'grnd beam',
        'gradebeam', 'groundbeam', 'sub beam',
    ]),
    # Beams — lintel
    ('Lintel',            [
        'lintel', 'lintol', 'lintle', 'lntel', 'lintle beam',
        'lintle bm', 'lintel beam', 'window lintel', 'door lintel',
        'lintol beam', 'ring beam', 'ring bm', 'ring-beam',
        'spandrel beam', 'spandrel bm',
    ]),

    # Slabs — transfer
    ('Transfer Slab',     [
        'transfer slab', 'transfer plate', 'transfer-slab', 'xfer slab',
        'trans slab', 'trans. slab', 't/slab', 'transfer slb',
        'transferr slab', 'tranfer slab', 'trnsfr slab',
    ]),
    # Slabs — ground floor
    ('Ground Floor Slab', [
        'ground floor slab', 'ground slab', 'slab on grade', 'sog',
        'slab-on-grade', 'ground bearing', 'gfs', 'g/slab', 'g.slab',
        'ground floor slb', 'gr floor slab', 'gr. floor slab',
        'ground flr slab', 'grnd floor slab', 'grnd slab',
        'slab on grnd', 'slab on ground', 'slab-on-ground',
        'ground flr slb', 'grd slab', 'soil slab', 'g.f. slab',
    ]),
    # Slabs — basement
    ('Basement Slab',     [
        'basement slab', 'basement floor', 'b/slab', 'bsmt slab',
        'bsmnt slab', 'basement slb', 'basement flr', 'blw grade slab',
        'below grade slab', 'sub slab', 'below ground slab',
    ]),
    # Slabs — podium
    ('Podium Slab',       [
        'podium slab', 'podium deck', 'podium plate', 'podium flr',
        'podium floor', 'podm slab', 'podium slb', 'podeum slab',
        'podium lvl slab',
    ]),
    # Slabs — hollowcore
    ('Hollowcore Slab',   [
        'hollowcore', 'hollow core', 'hc slab', 'hcs', 'precast hollow',
        'voided slab', 'cored slab', 'plank slab', 'hollow-core',
        'hollow core slab', 'hollowcore slab', 'h.c. slab', 'hc floor',
        'hollow plank', 'precast plank', 'voided biaxial', 'bubble deck',
        'bubbledeck', 'bubble slab', 'biaxial slab', 'hollow core plank',
        'hllowcore', 'hollowcor', 'hollw core', 'holow core',
    ]),
    # Slabs — waffle
    ('Waffle Slab',       [
        'waffle slab', 'waffle', 'coffered slab', 'coffered', 'two-way ribbed',
        'waffle floor', 'waf slab', 'coffered floor', 'grid slab',
        'waffle-slab', 'wafle slab', 'wafl slab', 'cofferred slab',
        'two way ribbed', '2-way ribbed', '2way ribbed',
    ]),
    # Slabs — ribbed / one-way
    ('Ribbed Slab',       [
        'ribbed slab', 'rib slab', 'one-way ribbed', 'joist slab',
        'ribbed floor', 'rib floor', 'one way ribbed', '1-way ribbed',
        '1way ribbed', 'ribbed-slab', 'ribb slab', 'ribded slab',
        'ribbed slb', 'joisted slab', 'joist floor',
    ]),
    # Slabs — composite deck
    ('Composite Deck',    [
        'composite deck', 'metal deck', 'decking', 'composite floor', 'steel deck',
        'compdeck', 'comp deck', 'comp. deck', 'metal decking', 'steel decking',
        'composite decking', 'profiled deck', 'profiled sheet', 'corrugated deck',
        'comflor', 'cofraplus', 'holorib', 'kingfloor', 'metfloor',
        'compostie deck', 'composit deck', 'metaldeck',
    ]),
    # Slabs — precast
    ('Precast Slab',      [
        'precast slab', 'precast plank', 'precast panel', 'precast floor',
        'double tee', 'double-tee', 'dt slab', 'pc slab', 'p.c. slab',
        'precast concrete slab', 'prefab slab', 'prefabricated slab',
        'tee beam slab', 'inverted tee', 'channel slab',
        'precast slb', 'precst slab', 'precast slabs', 'pre-cast slab',
        'pre cast slab', 'preccast slab',
    ]),
    # Ramps
    ('Ramp',              [
        'ramp', 'car ramp', 'vehicle ramp', 'parking ramp', 'access ramp',
        'disabled ramp', 'wheelchair ramp', 'ramp slab', 'sloped slab',
        'inclined slab', 'sloped floor', 'rampp', 'raamp',
    ]),
    # Stairs
    ('Stair',             [
        'stair', 'staircase', 'stair flight', 'landing', 'stairs',
        'flight', 'stairway', 'stair slab', 'stairwell', 'stair waist',
        'flight slab', 'step slab', 'staircase slab', 'stair plate',
        'stair core', 'stair tower', 'escape stair', 'fire stair',
        'fire escape', 'external stair', 'stairflight', 'stairflght',
        'staris', 'starir', 'steair', 'stair slb', 'landng', 'landig',
    ]),
    # Roof slabs
    ('Roof Slab',         [
        'roof slab', 'roof deck', 'roof plate', 'roof slb', 'rooff slab',
        'roof floor', 'terrace slab', 'terrace floor', 'roof panel',
        'flat roof slab', 'roof structure slab', 'overhead slab',
    ]),
]

def _override_category(ifc_type, name, family, obj_type):
    """
    Return a more specific structural category from name/family keywords.
    Checks structural sub-types first, then non-structural overrides.
    Returns None if no override is needed.
    """
    combined = ' '.join([name, family, obj_type]).lower()

    # Structural sub-type overrides take priority over non-structural
    for category, keywords in STRUCTURAL_OVERRIDES:
        if any(kw in combined for kw in keywords):
            return category

    # Non-structural overrides (only if no structural match)
    if any(kw in combined for kw in GLAZING_KW):
        return 'Glazing/Curtain Wall'
    if any(kw in combined for kw in DOOR_KW):
        return 'Door'

    return None

# ── Slab type keywords ─────────────────────────────────────────────────────
HOLLOWCORE_KW = [
    'hollowcore', 'hollow core', 'hc slab', 'hcs', 'precast hollow',
    'cored slab', 'voided slab', 'plank', 'hollow-core', 'h.c. slab',
    'hollow plank', 'voided biaxial', 'bubble deck', 'bubbledeck',
    'bubble slab', 'biaxial slab', 'hollow core plank',
    'hllowcore', 'hollowcor', 'hollw core', 'holow core',
]
WAFFLE_KW = [
    'waffle', 'coffered', 'grid slab', 'two-way ribbed',
    'waffle slab', 'coffered slab', 'waf slab', 'cofferred',
    'two way ribbed', '2-way ribbed', 'wafle', 'wafl slab',
]
RIBBED_KW = [
    'ribbed', 'rib slab', 'one-way ribbed', 't-beam', 'joist',
    'ribbed slab', 'rib floor', 'one way ribbed', '1-way ribbed',
    'joisted', 'joist slab', 'ribded', 'ribb slab',
]
PRECAST_KW = [
    'precast', 'prefab', 'prefabricated', 'pc ', 'p.c.', 'factory made',
    'pre-cast', 'pre cast', 'precst', 'preccast', 'factory-made',
    'factory produced', 'offsite', 'off-site', 'off site',
]
GIRDER_KW = [
    'girder', 'plate girder', 'box girder', 'i-girder', 'steel beam',
    'plate gdr', 'box gdr', 'i girder', 'steel bm', 'plate-girder',
    'lattice girder', 'truss girder', 'grder', 'gider',
]

HOLLOWCORE_VOID = {
    150: 0.35, 200: 0.40, 250: 0.42, 265: 0.43,
    300: 0.45, 320: 0.46, 400: 0.48, 500: 0.50,
}

# ── Volume validation ranges (m³) ──────────────────────────────────────────
VOLUME_RANGES = {
    'IfcColumn': (0.001, 50),
    'IfcBeam': (0.001, 100),
    'IfcSlab': (0.01, 5000),
    'IfcWall': (0.01, 500),
    'IfcWallStandardCase': (0.01, 500),
    'IfcFooting': (0.01, 1000),
    'IfcPile': (0.01, 100),
    'IfcMember': (0.001, 50),
    'IfcPlate': (0.0001, 10),
    'IfcRoof': (0.01, 2000),
    'IfcStair': (0.01, 200),
}


# ── Helper functions ────────────────────────────────────────────────────────

def _get_density(material_name):
    """Get material density from name."""
    if not material_name:
        return MATERIAL_DENSITIES['default']
    ml = material_name.lower()
    for key, d in MATERIAL_DENSITIES.items():
        if key in ml:
            return d
    if any(m in ml for m in ['metal', 'steel', 'iron']):
        return MATERIAL_DENSITIES['steel']
    if any(m in ml for m in ['concrete', 'rcc', 'pcc']):
        return MATERIAL_DENSITIES['concrete']
    return MATERIAL_DENSITIES['default']


def _identify_slab_type(name, family, obj_type):
    """Identify special slab type from naming."""
    text = f"{name} {family} {obj_type}".lower()
    if any(kw in text for kw in HOLLOWCORE_KW):
        return 'HOLLOWCORE'
    if any(kw in text for kw in WAFFLE_KW):
        return 'WAFFLE'
    if any(kw in text for kw in RIBBED_KW):
        return 'RIBBED'
    return 'SOLID'


def _is_steel_element(ifc_type, material_name, name, family):
    """Check if element is steel/metal."""
    text = f"{name} {family} {material_name}".lower()
    steel_types = {'IfcMember', 'IfcPlate', 'IfcReinforcingBar',
                   'IfcReinforcingMesh', 'IfcTendon', 'IfcTendonAnchor',
                   'IfcMechanicalFastener', 'IfcFastener'}
    if ifc_type in steel_types:
        return True
    kw = ['steel', 'metal', 'iron', 'rebar', 'reinforcement',
          'stainless', 'galvanized', 'galvanised']
    if any(k in text for k in kw):
        return True
    if any(k in text for k in GIRDER_KW):
        return True
    # Recognised structural-steel section name (UB/UC/CHS/… and the ASB/SFB/IFB
    # Slimflor family), so a steel beam whose IFC material is generic/missing
    # isn't mis-priced as concrete.
    if is_structural_steel_name(f"{name} {family}"):
        return True
    return False


def _is_structural_steel_element(ifc_type, name, family, obj_type):
    """Return True for structural steel members (not rebar/tendon-like items)."""
    rebar_types = {'IfcReinforcingBar', 'IfcReinforcingMesh', 'IfcTendon', 'IfcTendonAnchor'}
    if ifc_type in rebar_types:
        return False

    text = f"{name} {family} {obj_type}".lower()
    rebar_like = any(k in text for k in [
        'rebar', 'reinforcement', 'reinforcing', 'mesh',
        'tendon', 'strand', 'post-tension', 'post tension',
    ])
    if rebar_like:
        return False

    if is_structural_steel_name(f"{name} {family} {obj_type}"):
        return True

    # IfcMember and IfcPlate are always structural steel in IFC schema
    if ifc_type in {'IfcMember', 'IfcPlate'}:
        return True

    # For all other types, only classify as structural steel if name/family
    # explicitly signals a section profile — do not default to True.
    return False


def _is_precast(name, family, obj_type):
    """Check if element is precast."""
    text = f"{name} {family} {obj_type}".lower()
    return any(kw in text for kw in PRECAST_KW)


def _validate_volume(volume, ifc_type):
    """Validate volume against reasonable ranges. Returns adjusted volume."""
    min_vol, max_vol = VOLUME_RANGES.get(ifc_type, (0.0001, 10000))
    if volume < 0:
        volume = abs(volume)
    # Only clamp if wildly above max (10× headroom) to avoid rejecting
    # legitimate large elements (e.g. raft slabs, Grand Canal floor plates).
    if volume > max_vol * 10:
        return max_vol
    return volume


# ═══════════════════════════════════════════════════════════════════════════
# IFC PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════

class IFCProcessor:
    """Full-featured IFC quantity extractor with 3D geometry output."""

    def __init__(self, ifc_path, progress_callback=None):
        self.ifc_path = ifc_path
        self.ifc_file = None
        self.progress = progress_callback or (lambda p, m: None)
        self.unit_scale_length = 1.0
        self.unit_scale_area = 1.0
        self.unit_scale_volume = 1.0
        self.elements = []
        self.geometry = []

        # Statistics
        self.stats = defaultdict(int)
        self.geom_types_found = defaultdict(int)
        self.opening_volumes = {}  # Cache for wall openings

        # Shape cache: GlobalId -> {'vertices', 'triangles', 'volume', 'bbox'}
        # Populated on first access, reused by 3D viewer + volume extraction.
        self.shape_cache = {}
        self._geom_settings = None

        # Lazy-mesh state (used only when process(defer_viewer_meshes=True)).
        self._defer_viewer = False
        self._deferred_gids = []

    def process(self, defer_viewer_meshes=False):
        """Full extraction pipeline. Returns (DataFrame, geometry_list).

        defer_viewer_meshes=False (default): output is identical to before —
        every element gets a tessellated viewer mesh — but tessellation now runs
        in a multi-core batch instead of serially per element.

        defer_viewer_meshes=True: elements whose volume already comes from QTO
        metadata skip tessellation on the critical path (their volume/quantities
        are unaffected, and they are volume_sum_safe so join-overlap ignores
        them). Their cosmetic 3D-viewer mesh is left for build_viewer_meshes(),
        so quantities return without waiting on geometry.
        """
        self._defer_viewer = bool(defer_viewer_meshes)
        self.progress(5, "Loading IFC file...")
        self.ifc_file = ifcopenshell.open(self.ifc_path)
        self._detect_units()

        self.progress(15, "Extracting structural elements...")
        self._extract_elements()

        self.progress(78, "Deducting element join overlaps...")
        self._deduct_join_overlaps()

        self.progress(82, "Extracting 3D mesh geometry...")
        self._extract_mesh_geometry()

        self.progress(90, "Building data tables...")
        df = self._build_dataframe()

        # Unit sanity check — if avg structural element volume < 0.01 m³
        # it likely means IFC declared length in mm but volume was not correctly scaled.
        if len(df) > 0:
            avg_vol = df['volume_m3'].median()
            if 0 < avg_vol < 0.01:
                self.stats['unit_warning'] = (
                    f"Median element volume is {avg_vol:.6f} m³ — this is unusually small. "
                    f"IFC may have a unit mismatch (e.g. length in mm, volume not scaled). "
                    f"Check your IFC export settings."
                )

        self.progress(100, "Done")
        # Strip Revit instance IDs from geometry names to match the stripped df names,
        # so emission lookups by name in the 3D viewer always succeed.
        for g in self.geometry:
            g['name'] = self._strip_instance_id(g.get('name', ''))
        return df, self.geometry

    def _get_geom_settings(self):
        """Lazy-init shared ifcopenshell.geom settings."""
        if self._geom_settings is None:
            try:
                s = ifcopenshell.geom.settings()
                s.set("use-world-coords", True)
                s.set("weld-vertices", True)
                s.set("mesher-linear-deflection", 0.005)
                self._geom_settings = s
            except Exception:
                self._geom_settings = False  # sentinel: geom unavailable
        return self._geom_settings or None

    def _build_shape(self, element):
        """
        Build (or fetch from cache) the tessellated shape for an element.
        Returns dict with vertices, triangles, volume (m³), bbox dims (m), or None.

        This is the AUTHORITATIVE volume source: ifcopenshell.geom applies all
        transformations (MappedItem targets, local placements), resolves every
        geometry type (BRep, ExtrudedArea, SweptDisk, FaceSets), executes all
        CSG boolean operations (openings, cuts, joins), and returns the same
        net solid that the BIM authoring tool computes. Matches Revit/Tekla
        NetVolume to within tessellation tolerance (~0.1%).
        """
        gid = getattr(element, 'GlobalId', None)
        if gid and gid in self.shape_cache:
            return self.shape_cache[gid]

        settings = self._get_geom_settings()
        if settings is None:
            return None

        try:
            shape = ifcopenshell.geom.create_shape(settings, element)
            mesh = shape.geometry
            result = self._mesh_to_record(mesh.verts, mesh.faces)
            if result is None:
                return None
            if gid:
                self.shape_cache[gid] = result
            return result
        except Exception:
            return None

    def _mesh_to_record(self, verts, faces):
        """Convert flat ifcopenshell verts/faces into the shape_cache record:
        {'vertices', 'triangles', 'volume', 'bbox'}. Volume via signed
        tetrahedron sum (already metres — use-world-coords applies the unit
        scale). Returns None for degenerate meshes. Used by both the serial
        _build_shape and the parallel _tessellate_batch so they agree exactly.
        """
        try:
            if len(verts) < 9 or len(faces) < 3:
                return None

            # Flat [x0,y0,z0, x1,...] → list of (x,y,z) tuples
            n_v = len(verts) // 3
            vertices = [(verts[3*j], verts[3*j+1], verts[3*j+2]) for j in range(n_v)]
            n_f = len(faces) // 3
            triangles = [(faces[3*j], faces[3*j+1], faces[3*j+2]) for j in range(n_f)]

            # Tetrahedra are formed against a reference apex. With use-world-coords,
            # that apex is the model origin (0,0,0) — but a project positioned far
            # from origin (e.g. survey coordinates in the hundreds of metres) blows
            # up each tetrahedron's lever arm, so any tiny topology defect in the
            # BRep (a non-manifold edge, one flipped-winding face) gets amplified
            # into a wildly wrong volume even though the mesh's own bbox is tiny.
            # Using the mesh's own first vertex as the apex keeps every lever arm
            # local to the element's own size, eliminating that amplification.
            ox, oy, oz = vertices[0]
            vol6 = 0.0
            for a, b, c in triangles:
                if a >= n_v or b >= n_v or c >= n_v:
                    continue
                x1, y1, z1 = vertices[a]
                x2, y2, z2 = vertices[b]
                x3, y3, z3 = vertices[c]
                x1 -= ox; y1 -= oy; z1 -= oz
                x2 -= ox; y2 -= oy; z2 -= oz
                x3 -= ox; y3 -= oy; z3 -= oz
                vol6 += (x1 * (y2 * z3 - y3 * z2)
                         - x2 * (y1 * z3 - y3 * z1)
                         + x3 * (y1 * z2 - y2 * z1))
            volume = abs(vol6) / 6.0

            xs = [v[0] for v in vertices]
            ys = [v[1] for v in vertices]
            zs = [v[2] for v in vertices]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_z, max_z = min(zs), max(zs)
            bbox = {
                'min_x': min_x, 'max_x': max_x,
                'min_y': min_y, 'max_y': max_y,
                'min_z': min_z, 'max_z': max_z,
                'length': max_x - min_x,
                'width':  max_y - min_y,
                'height': max_z - min_z,
            }
            return {
                'vertices': vertices,
                'triangles': triangles,
                'volume': volume,
                'bbox': bbox,
            }
        except Exception:
            return None

    def _needs_tessellation_now(self, element):
        """Defer-mode batch selector: must this element be tessellated before the
        DataFrame is built? Yes when its mesh is the volume source (no QTO volume)
        or its dimensions are incomplete (bbox is needed for the width/depth that
        drive row grouping). Only QTO elements with all dims present can be left
        to the cosmetic viewer pass. Mirrors the defer gate in _process_element so
        the parallel batch covers exactly the meshes the DataFrame depends on."""
        try:
            q = self._extract_from_ifc_quantities(element)
        except Exception:
            return True
        if q['volume'] <= 0:
            return True
        return not (q['length'] > 0 and q['width'] > 0 and q['height'] > 0)

    def _tessellate_batch(self, elements):
        """Populate shape_cache for `elements` using the multi-core geometry
        iterator. Verified to yield meshes identical to serial create_shape.
        Falls back silently on any error — the per-element _build_shape still
        tessellates on a cache miss, so correctness never depends on this."""
        if not elements:
            return
        settings = self._get_geom_settings()
        if settings is None:
            return
        try:
            import multiprocessing
            n = max(1, (multiprocessing.cpu_count() or 2) - 1)
        except Exception:
            n = 1
        try:
            it = ifcopenshell.geom.iterator(settings, self.ifc_file, n,
                                            include=list(elements))
            if not it.initialize():
                return
            while True:
                shape = it.get()
                try:
                    gid = getattr(shape, 'guid', None)
                    if gid and gid not in self.shape_cache:
                        rec = self._mesh_to_record(shape.geometry.verts,
                                                   shape.geometry.faces)
                        if rec is not None:
                            self.shape_cache[gid] = rec
                except Exception:
                    pass
                if not it.next():
                    break
        except Exception:
            pass

    def build_viewer_meshes(self, progress_callback=None):
        """Tessellate the 3D-viewer meshes deferred during a
        process(defer_viewer_meshes=True) run, and attach them to self.geometry
        in place (a caller holding the geometry reference sees the meshes
        appear). No effect on any quantity. Safe no-op when nothing deferred."""
        if not self._deferred_gids:
            return
        pending = []
        for g in self._deferred_gids:
            if not g or g in self.shape_cache:
                continue
            try:
                el = self.ifc_file.by_guid(g)
                if el is not None:
                    pending.append(el)
            except Exception:
                pass
        if pending:
            self._tessellate_batch(pending)
        self._extract_mesh_geometry()
        self._deferred_gids = []

    def _extract_mesh_geometry(self):
        """Attach cached tessellated mesh to 3D viewer geometry entries."""
        gid_to_idx = {}
        for i, elem in enumerate(self.elements):
            gid = elem.get('global_id', '')
            if gid:
                gid_to_idx[gid] = i

        processed = 0
        for gid, shape in self.shape_cache.items():
            idx = gid_to_idx.get(gid)
            if idx is None or idx >= len(self.geometry):
                continue
            self.geometry[idx]['vertices'] = shape['vertices']
            self.geometry[idx]['triangles'] = shape['triangles']
            processed += 1

        self.stats['mesh_extracted'] = processed

    # ── Unit detection (3 methods with verification) ─────────────────────

    def _detect_units(self):
        detected = None

        # Method 1: Direct project unit
        try:
            lu = unit_util.get_project_unit(self.ifc_file, "LENGTHUNIT")
            if lu:
                name = getattr(lu, 'Name', None)
                prefix = getattr(lu, 'Prefix', None)
                if name == 'METRE':
                    if prefix == 'MILLI':
                        detected = 0.001
                    elif prefix == 'CENTI':
                        detected = 0.01
                    else:
                        detected = 1.0
                elif name == 'FOOT':
                    detected = 0.3048
                elif name == 'INCH':
                    detected = 0.0254
        except Exception:
            pass

        # Method 2: calculate_unit_scale as verification/fallback
        try:
            calc_scale = unit_util.calculate_unit_scale(self.ifc_file)
            if detected is None:
                detected = calc_scale
            elif abs(detected - calc_scale) > 0.0001:
                detected = calc_scale  # Trust calc if mismatch
        except Exception:
            pass

        # Method 3: IfcProject UnitsInContext
        if detected is None:
            try:
                projects = self.ifc_file.by_type('IfcProject')
                if projects:
                    project = projects[0]
                    if hasattr(project, 'UnitsInContext') and project.UnitsInContext:
                        for unit in project.UnitsInContext.Units:
                            if hasattr(unit, 'UnitType') and unit.UnitType == 'LENGTHUNIT':
                                if hasattr(unit, 'Prefix') and unit.Prefix == 'MILLI':
                                    detected = 0.001
                                break
            except Exception:
                pass

        if detected is None:
            detected = 0.001  # Default: assume mm

        self.unit_scale_length = detected

        # Detect area unit separately — IFC may declare AREAUNIT = SQUARE_METRE
        # even when LENGTHUNIT = MILLIMETRE (common in Revit exports with base quantities)
        area_scale = None
        try:
            au = unit_util.get_project_unit(self.ifc_file, "AREAUNIT")
            if au:
                aname = getattr(au, 'Name', None)
                aprefix = getattr(au, 'Prefix', None)
                if aname in ('SQUARE_METRE', 'METRE') and aprefix is None:
                    area_scale = 1.0
        except Exception:
            pass
        if area_scale is None:
            try:
                projects = self.ifc_file.by_type('IfcProject')
                if projects:
                    for unit in (projects[0].UnitsInContext.Units if projects[0].UnitsInContext else []):
                        if hasattr(unit, 'UnitType') and unit.UnitType == 'AREAUNIT':
                            aprefix = getattr(unit, 'Prefix', None)
                            aname = getattr(unit, 'Name', None)
                            if aname in ('SQUARE_METRE',) and aprefix is None:
                                area_scale = 1.0
                            break
            except Exception:
                pass
        self.unit_scale_area = area_scale if area_scale is not None else detected ** 2

        # Detect volume unit separately — IFC may declare VOLUMEUNIT = CUBIC_METRE
        # even when LENGTHUNIT = MILLIMETRE (common in Revit exports with base quantities)
        vol_scale = None
        try:
            vu = unit_util.get_project_unit(self.ifc_file, "VOLUMEUNIT")
            if vu:
                vname = getattr(vu, 'Name', None)
                vprefix = getattr(vu, 'Prefix', None)
                if vname == 'CUBIC_METRE':
                    vol_scale = 1.0
                elif vname == 'METRE':
                    vol_scale = 1.0
        except Exception:
            pass
        if vol_scale is None:
            try:
                projects = self.ifc_file.by_type('IfcProject')
                if projects:
                    for unit in (projects[0].UnitsInContext.Units if projects[0].UnitsInContext else []):
                        if hasattr(unit, 'UnitType') and unit.UnitType == 'VOLUMEUNIT':
                            vprefix = getattr(unit, 'Prefix', None)
                            vname = getattr(unit, 'Name', None)
                            if vname in ('CUBIC_METRE',) and vprefix is None:
                                vol_scale = 1.0
                            break
            except Exception:
                pass
        self.unit_scale_volume = vol_scale if vol_scale is not None else detected ** 3

    # ── Element extraction ───────────────────────────────────────────────

    def _extract_elements(self):
        all_elems = []
        seen = set()
        for etype in STRUCTURAL_TYPES:
            try:
                for elem in self.ifc_file.by_type(etype, include_subtypes=True):
                    gid = getattr(elem, 'GlobalId', id(elem))
                    if gid not in seen:
                        seen.add(gid)
                        all_elems.append(elem)
            except Exception:
                pass

        # ── Parallel geometry pre-pass ────────────────────────────────────
        # Tessellate, in one multi-core batch, every element that will need a
        # mesh, so the per-element loop below hits the shape cache instead of
        # tessellating serially. In defer mode, elements whose volume comes from
        # QTO metadata are left out (their mesh is viewer-only) and built later
        # by build_viewer_meshes(); their quantities don't wait on geometry.
        if self._defer_viewer:
            batch = [e for e in all_elems if self._needs_tessellation_now(e)]
        else:
            batch = all_elems
        self.progress(20, "Tessellating geometry (parallel)...")
        self._tessellate_batch(batch)

        total = len(all_elems)
        for i, elem in enumerate(all_elems):
            if i % 20 == 0:
                pct = 22 + int((i / max(total, 1)) * 56)
                self.progress(pct, f"Processing element {i+1}/{total}")
            self._process_element(elem)

    def _process_element(self, element):
        ifc_type = element.is_a()
        name = getattr(element, 'Name', '') or ''
        obj_type = getattr(element, 'ObjectType', '') or ''
        family = ''
        try:
            etype = element_util.get_type(element)
            if etype:
                family = getattr(etype, 'Name', '') or ''
            if not family:
                family = obj_type or name or 'Unknown'
        except Exception:
            family = name or 'Unknown'

        category = CATEGORY_MAP.get(ifc_type, 'Other')
        override = _override_category(ifc_type, name, family, obj_type)
        if override:
            category = override
        # IfcSlab with PredefinedType BASESLAB is a foundation/base slab
        if ifc_type == 'IfcSlab' and category in ('Slab/Floor', 'Other'):
            pdt = getattr(element, 'PredefinedType', None)
            if pdt and str(pdt).upper() in ('BASESLAB',):
                category = 'Raft Foundation'
        level = self._get_storey(element)
        material = self._get_material(element)

        # ══════════════════════════════════════════════════════════════════
        # VOLUME EXTRACTION CASCADE
        #   M1: QTO Net/Gross (min rule) → authoritative, join-safe sum
        #   M0: ifcopenshell shape mesh  → applies openings, but per-element
        #                                  (sum double-counts at joins → deducted
        #                                  later in _deduct_join_overlaps)
        #   M2/M3: psets (min rule)      → fallback metadata
        #   M4: manual geometry parse    → last-ditch approximation
        #   M5: dimension calculation    → final fallback
        #
        # Flags produced:
        #   needs_opening_deduction  = True → subtract wall openings manually
        #   needs_void_deduction     = True → apply hollowcore/waffle factors
        #   volume_sum_safe          = True → summing across elements won't
        #                                     double-count element joins
        # ══════════════════════════════════════════════════════════════════

        # Method 1: IfcElementQuantity — min(Net, Gross) rule
        qto = self._extract_from_ifc_quantities(element)
        volume = qto['volume']
        area = qto['area']
        length = qto['length']
        width = qto['width']
        height = qto['height']
        thickness = qto['thickness']

        # Default flags: as if nothing known
        needs_opening_deduction = True
        needs_void_deduction = True
        volume_sum_safe = False
        source = ""

        if volume > 0:
            if qto['used_net']:
                source = "qto_net"
                needs_opening_deduction = False   # openings already subtracted
                volume_sum_safe = True            # min rule picked net (≤ gross)
            elif qto['used_gross']:
                source = "qto_gross"
                needs_opening_deduction = True    # gross has no opening subtraction
                volume_sum_safe = True            # gross is each element's own vol

        # Method 0: Shape-mesh via ifcopenshell.geom.
        # Mesh reflects opening booleans but not element-to-element joins
        # (Revit Join Geometry is NOT baked into per-element meshes for all types).
        # In defer mode the mesh can be skipped here (built later by
        # build_viewer_meshes) ONLY when it would not change the DataFrame:
        #   • volume already came from QTO metadata (mesh volume is discarded), and
        #   • length/width/height are all already known, so the bbox dimension
        #     fallback below wouldn't fire (width/depth drive row grouping).
        # Such an element is also volume_sum_safe, so join-overlap never reads
        # its bbox. Any element missing a volume or a dimension is tessellated
        # now — exactly as before — keeping output byte-identical.
        _can_defer = (self._defer_viewer and volume > 0
                      and length > 0 and width > 0 and height > 0)
        if _can_defer:
            shape = None
            _gid = getattr(element, 'GlobalId', '')
            if _gid:
                self._deferred_gids.append(_gid)
        else:
            shape = self._build_shape(element)
        shape_vol = shape['volume'] if shape else 0.0
        bbox = shape['bbox'] if shape else None

        if volume == 0 and shape_vol > 0:
            volume = shape_vol
            source = "shape"
            needs_opening_deduction = False   # mesh has openings subtracted
            volume_sum_safe = False           # mesh may over-count at joins

        # Bbox dims as fallback for missing dimensions
        if bbox:
            if length == 0: length = bbox['length']
            if width == 0:  width = bbox['width']
            if height == 0: height = bbox['height']

        # Method 2: Property sets via util
        pset_data = self._extract_quantities_from_psets(
            self._get_all_psets(element))
        if volume == 0 and pset_data['volume'] > 0:
            volume = pset_data['volume']
            if pset_data['used_net']:
                source = "pset_net"
                needs_opening_deduction = False
                volume_sum_safe = True
            else:
                source = "pset_gross"
                needs_opening_deduction = True
                volume_sum_safe = True
        if area == 0:
            area = pset_data['area']
        if length == 0:
            length = pset_data['length']
        if width == 0:
            width = pset_data['width']
        if height == 0:
            height = pset_data['height']
        if thickness == 0:
            thickness = pset_data['thickness']

        # Method 3: Type definition properties
        type_data = self._extract_quantities_from_psets(
            self._get_type_psets(element))
        if volume == 0 and type_data['volume'] > 0:
            volume = type_data['volume']
            if type_data['used_net']:
                source = "type_net"
                needs_opening_deduction = False
                volume_sum_safe = True
            else:
                source = "type_gross"
                needs_opening_deduction = True
                volume_sum_safe = True
        if area == 0:
            area = type_data['area']

        # Method 4: Geometry representation
        rep_data = self._extract_from_representation(element)
        if volume == 0 and rep_data.get('volume', 0) > 0:
            volume = rep_data['volume']
            source = "geom"
        if area == 0 and rep_data.get('area', 0) > 0:
            area = rep_data['area']
        if length == 0:
            length = rep_data.get('length', 0)
        if width == 0:
            width = rep_data.get('width', 0)
        if height == 0:
            height = rep_data.get('height', 0)

        # Material layer thickness for slabs/walls
        if thickness == 0 and ifc_type in ('IfcSlab', 'IfcWall', 'IfcWallStandardCase', 'IfcRoof'):
            mat_t = self._get_material_thickness(element)
            if mat_t > 0:
                thickness = mat_t
            elif rep_data.get('height', 0) > 0 and rep_data.get('height', 0) < 1:
                thickness = rep_data['height']

        # Method 5: Calculate from dimensions
        if volume == 0:
            if length > 0 and width > 0 and height > 0:
                volume = length * width * height
                source = "calc_lwh"
            elif area > 0 and thickness > 0:
                volume = area * thickness
                source = "calc_at"
            elif area > 0 and height > 0:
                volume = area * height
                source = "calc_ah"

        # ── Wall opening deduction ────────────────────────────────────────
        # Subtract IfcRelVoidsElement openings only when the source value
        # doesn't already have them subtracted.
        net_volume = volume
        opening_volume = 0.0
        if (ifc_type in ('IfcWall', 'IfcWallStandardCase')
                and volume > 0 and needs_opening_deduction):
            opening_volume = self._get_opening_volume(element)
            if 0 < opening_volume < volume:
                net_volume = volume - opening_volume

        # ── Volume validation ─────────────────────────────────────────────
        volume = _validate_volume(volume, ifc_type)
        if net_volume > volume:
            net_volume = volume

        # ── Slab type and void deductions ─────────────────────────────────
        slab_type = 'SOLID'
        void_vol = 0.0
        adjusted_volume = net_volume

        if ifc_type in ('IfcSlab', 'IfcPlate', 'IfcRoof'):
            slab_type = _identify_slab_type(name, family, obj_type)
            # Apply void factors only if QTO source hasn't already accounted
            # for them. Revit net/gross for hollowcore/waffle slabs usually
            # reflects the solid envelope, so ratios are still applied.
            # When shape mesh is used we skip — the tessellated mesh reflects
            # any modelled voids directly.
            apply_void = needs_void_deduction and source != 'shape'
            if apply_void:
                if slab_type == 'HOLLOWCORE' and net_volume > 0:
                    t_mm = thickness * 1000 if thickness > 0 else 200
                    ratio = self._hc_void(t_mm)
                    void_vol = net_volume * ratio
                    adjusted_volume = net_volume - void_vol
                elif slab_type == 'WAFFLE' and net_volume > 0:
                    void_vol = net_volume * 0.35
                    adjusted_volume = net_volume - void_vol
                elif slab_type == 'RIBBED' and net_volume > 0:
                    void_vol = net_volume * 0.55
                    adjusted_volume = net_volume - void_vol

        # ── Steel detection and weight ────────────────────────────────────
        is_steel = _is_steel_element(ifc_type, material, name, family)
        is_structural_steel = False
        density = MATERIAL_DENSITIES['steel'] if is_steel else _get_density(material)
        weight_kg = 0.0
        if is_steel:
            is_structural_steel = _is_structural_steel_element(ifc_type, name, family, obj_type)
            weight_kg = adjusted_volume * MATERIAL_DENSITIES['steel']
        elif ifc_type in ('IfcReinforcingBar', 'IfcReinforcingMesh', 'IfcTendon'):
            weight_kg = adjusted_volume * MATERIAL_DENSITIES['steel']
            is_steel = True
            is_structural_steel = False

        is_pc = _is_precast(name, family, obj_type)

        # ── Track stats ───────────────────────────────────────────────────
        if source:
            # Collapse qto_net→qto, pset_net→pset, type_net→type for stats
            stat_key = source.split("_")[0]
            self.stats[f'volume_from_{stat_key}'] += 1
        else:
            self.stats['volume_zero'] += 1

        # ── Geometry for 3D viewer ────────────────────────────────────────
        pos = self._get_position(element)
        geo = {
            'x': pos[0], 'y': pos[1], 'z': pos[2],
            'width': width or 0.3,
            'depth': length or thickness or 0.3,
            'height': height or 0.3,
            'category': category,
            'name': name,
            'level': level,
        }
        self.geometry.append(geo)

        # ── Store element data ────────────────────────────────────────────
        self.elements.append({
            'global_id': getattr(element, 'GlobalId', ''),
            'ifc_type': ifc_type,
            'category': category,
            'name': name,
            'family': family,
            'level': level,
            'material': material,
            'volume_m3': round(adjusted_volume, 4),
            'gross_volume_m3': round(volume, 4),
            'net_volume_m3': round(net_volume, 4),
            'void_volume_m3': round(void_vol, 4),
            'opening_volume_m3': round(opening_volume, 4),
            'overlap_deduction_m3': 0.0,  # filled in by _deduct_join_overlaps
            'area_m2': round(area, 4),
            'density_kg_m3': density,
            'weight_kg': round(weight_kg, 2),
            'is_steel': is_steel,
            'is_structural_steel': is_structural_steel,
            'is_precast': is_pc,
            'slab_type': slab_type,
            'width': round(width, 3),
            'depth': round(length or thickness, 3),
            'height': round(height, 3),
            'thickness': round(thickness, 3),
            'pos_x': round(pos[0], 2),
            'pos_y': round(pos[1], 2),
            'pos_z': round(pos[2], 2),
            'source': source,
            'volume_sum_safe': volume_sum_safe,
            '_bbox': bbox,  # world-coord AABB for overlap pass; stripped later
        })

    # ═══════════════════════════════════════════════════════════════════════
    # JOIN-OVERLAP DEDUCTION
    # ═══════════════════════════════════════════════════════════════════════

    # Category → structural group for join-overlap deduction.
    # Walls are intentionally omitted: in Revit, wall meshes typically already
    # carve out openings where columns/beams pass through, so the raw IFC shape
    # does not double-count. Deducting on an AABB overlap would over-subtract.
    _JOIN_GROUP = {
        'Foundation/Footing': 'found', 'Pile': 'found', 'Pile Cap': 'found',
        'Raft Foundation': 'found', 'Pad Footing': 'found', 'Strip Footing': 'found',
        'Column': 'col',
        'Beam': 'beam', 'Transfer Beam': 'beam', 'Ground Beam': 'beam', 'Lintel': 'beam',
        'Slab/Floor': 'slab', 'Roof': 'slab', 'Roof Slab': 'slab',
        'Transfer Slab': 'slab', 'Ground Floor Slab': 'slab', 'Basement Slab': 'slab',
        'Podium Slab': 'slab', 'Hollowcore Slab': 'slab', 'Waffle Slab': 'slab',
        'Ribbed Slab': 'slab', 'Composite Deck': 'slab', 'Precast Slab': 'slab',
        'Ramp': 'slab',
    }

    # Allowed deduction pairs: (winner_group, loser_group) — winner keeps full
    # volume, loser is reduced by the AABB overlap.
    _JOIN_DEDUCT_PAIRS = {
        ('found', 'col'),    # column base absorbed into footing
        ('col', 'beam'),     # column extends through beam-column joint
        ('col', 'slab'),     # column punches through slab
        ('beam', 'slab'),    # band beam extended into slab
    }

    def _deduct_join_overlaps(self):
        """
        Subtract double-counted volumes at element-to-element joins.

        When the authoring tool's Join Geometry has not been baked into the
        source volume (shape-mesh path), summing per-element volumes over-
        counts the concrete shared between joined elements — most notably
        band beams extending into slabs.

        This pass computes pairwise AABB overlap between elements whose
        source is not join-safe, restricted to a whitelist of physically-
        meaningful join pairs (beam-slab, column-slab, column-beam,
        column-foundation). Walls are excluded — their IFC meshes typically
        already reflect the carve-out for adjacent columns/beams.
        """
        cands = []
        for idx, e in enumerate(self.elements):
            if e.get('volume_sum_safe'):
                continue
            if e.get('is_steel') or e.get('ifc_type') in (
                'IfcReinforcingBar', 'IfcReinforcingMesh', 'IfcTendon'):
                continue
            bbox = e.get('_bbox')
            vol = e.get('volume_m3', 0)
            if not bbox or vol <= 0:
                continue
            group = self._JOIN_GROUP.get(e.get('category', ''))
            if group is None:
                continue
            cands.append((idx, e, bbox, group))

        if len(cands) < 2:
            return

        cands.sort(key=lambda x: x[2]['min_z'])
        deductions = defaultdict(float)
        pairs_checked = 0
        pairs_overlap = 0

        for i in range(len(cands)):
            idx_a, elem_a, bb_a, grp_a = cands[i]
            max_z_a = bb_a['max_z']
            va = elem_a['volume_m3']

            for j in range(i + 1, len(cands)):
                idx_b, elem_b, bb_b, grp_b = cands[j]
                if bb_b['min_z'] >= max_z_a:
                    break  # sorted by min_z → no further Z overlap possible
                pairs_checked += 1

                # Only deduct if this pair is a recognised structural join
                if (grp_a, grp_b) in self._JOIN_DEDUCT_PAIRS:
                    loser_idx = idx_b
                elif (grp_b, grp_a) in self._JOIN_DEDUCT_PAIRS:
                    loser_idx = idx_a
                else:
                    continue

                ox = min(bb_a['max_x'], bb_b['max_x']) - max(bb_a['min_x'], bb_b['min_x'])
                if ox <= 0:
                    continue
                oy = min(bb_a['max_y'], bb_b['max_y']) - max(bb_a['min_y'], bb_b['min_y'])
                if oy <= 0:
                    continue
                oz = min(bb_a['max_z'], bb_b['max_z']) - max(bb_a['min_z'], bb_b['min_z'])
                if oz <= 0:
                    continue

                overlap = ox * oy * oz
                vb = elem_b['volume_m3']
                overlap = min(overlap, va, vb)

                smaller = min(va, vb)
                if overlap < 1e-3 or overlap < 0.01 * smaller:
                    continue
                pairs_overlap += 1

                deductions[loser_idx] += overlap

        applied = 0
        total_deducted = 0.0
        for idx, d in deductions.items():
            elem = self.elements[idx]
            v = elem['volume_m3']
            if v <= 0:
                continue
            # Cap deduction: never remove more than 70% of original volume
            d_capped = min(d, v * 0.7)
            new_v = v - d_capped
            scale = new_v / v if v > 0 else 1.0
            elem['volume_m3'] = round(new_v, 4)
            elem['overlap_deduction_m3'] = round(d_capped, 4)
            if elem.get('weight_kg', 0) > 0:
                elem['weight_kg'] = round(elem['weight_kg'] * scale, 2)
            applied += 1
            total_deducted += d_capped

        self.stats['overlap_pairs_checked'] = pairs_checked
        self.stats['overlap_pairs_applied'] = pairs_overlap
        self.stats['overlap_elements_reduced'] = applied
        self.stats['overlap_total_m3'] = round(total_deducted, 2)

    # ═══════════════════════════════════════════════════════════════════════
    # METHOD 1: DIRECT IFC QUANTITY EXTRACTION
    # ═══════════════════════════════════════════════════════════════════════

    def _extract_from_ifc_quantities(self, element):
        result = {'volume': 0.0, 'net_volume': 0.0, 'gross_volume': 0.0,
                  'area': 0.0, 'length': 0.0, 'width': 0.0, 'height': 0.0,
                  'thickness': 0.0, 'used_net': False, 'used_gross': False}

        if not hasattr(element, 'IsDefinedBy'):
            return result

        try:
            for rel in element.IsDefinedBy:
                if not hasattr(rel, 'is_a') or not rel.is_a('IfcRelDefinesByProperties'):
                    continue
                pdef = rel.RelatingPropertyDefinition
                if not pdef or not pdef.is_a('IfcElementQuantity'):
                    continue
                for qty in pdef.Quantities:
                    nm = (qty.Name or '').lower()
                    try:
                        if qty.is_a('IfcQuantityVolume') and qty.VolumeValue:
                            v = qty.VolumeValue * self.unit_scale_volume
                            # Capture net and gross separately — min rule applied below
                            # to avoid double-counting Revit's Join Geometry which makes
                            # NetVolume include regions absorbed from adjacent elements.
                            if 'net' in nm:
                                result['net_volume'] = max(result['net_volume'], v)
                            elif 'gross' in nm:
                                result['gross_volume'] = max(result['gross_volume'], v)
                            elif result['net_volume'] == 0 and result['gross_volume'] == 0:
                                # Unlabelled volume — treat as net (IFC default)
                                result['net_volume'] = v
                        elif qty.is_a('IfcQuantityArea') and qty.AreaValue:
                            v = qty.AreaValue * self.unit_scale_area
                            if 'side' not in nm:
                                result['area'] = max(result['area'], v)
                        elif qty.is_a('IfcQuantityLength') and qty.LengthValue:
                            v = qty.LengthValue * self.unit_scale_length
                            if 'height' in nm:
                                result['height'] = max(result['height'], v)
                            elif 'width' in nm:
                                result['width'] = max(result['width'], v)
                            elif 'thick' in nm or 'depth' in nm:
                                result['thickness'] = max(result['thickness'], v)
                            else:
                                result['length'] = max(result['length'], v)
                    except Exception:
                        pass
        except Exception:
            pass

        # Apply min(Net, Gross) rule to eliminate Join-Geometry double-counting.
        # Rationale: when Revit exports a band beam or column-joined-to-slab, it
        # records NetVolume larger than GrossVolume because Net has absorbed the
        # joined region. Summing such Net values across elements double-counts
        # the shared concrete. Gross is each element's own volume — summing Gross
        # gives the correct total. When Net < Gross (normal opening subtraction),
        # Net is correct. min() picks the right one automatically.
        n = result['net_volume']
        g = result['gross_volume']
        if n > 0 and g > 0:
            if n <= g:
                result['volume'] = n
                result['used_net'] = True
            else:
                result['volume'] = g
                result['used_gross'] = True
        elif n > 0:
            result['volume'] = n
            result['used_net'] = True
        elif g > 0:
            result['volume'] = g
            result['used_gross'] = True
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # METHOD 2 & 3: PROPERTY SET EXTRACTION
    # ═══════════════════════════════════════════════════════════════════════

    def _get_all_psets(self, element):
        try:
            return element_util.get_psets(element)
        except Exception:
            return {}

    def _get_type_psets(self, element):
        try:
            etype = element_util.get_type(element)
            if etype:
                return element_util.get_psets(etype)
        except Exception:
            pass
        return {}

    def _extract_quantities_from_psets(self, psets):
        result = {'volume': 0.0, 'net_volume': 0.0, 'gross_volume': 0.0,
                  'area': 0.0, 'net_area': 0.0, 'gross_area': 0.0,
                  'length': 0.0, 'width': 0.0, 'height': 0.0,
                  'thickness': 0.0, 'perimeter': 0.0,
                  'used_net': False, 'used_gross': False}
        if not psets:
            return result

        for pset_name, props in psets.items():
            if not isinstance(props, dict):
                continue
            pset_lower = pset_name.lower()
            is_qto = any(k in pset_lower for k in ('qto', 'quantit', 'base'))

            for prop_name, value in props.items():
                if not isinstance(value, (int, float)) or value is None:
                    continue
                nl = prop_name.lower()
                scale_v = self.unit_scale_volume if is_qto else 1.0
                scale_l = self.unit_scale_length if is_qto else 1.0
                scale_a = self.unit_scale_area if is_qto else 1.0

                if any(v in nl for v in ('volume', 'vol', 'volumen')):
                    val = float(value) * scale_v
                    if 'net' in nl:
                        result['net_volume'] = max(result['net_volume'], val)
                    elif 'gross' in nl:
                        result['gross_volume'] = max(result['gross_volume'], val)
                    else:
                        result['volume'] = max(result['volume'], val)
                elif any(a in nl for a in ('area', 'fläche', 'superficie')):
                    val = float(value) * scale_a
                    if 'side' in nl:
                        pass  # skip side/face areas — not plan/floor area
                    elif 'net' in nl:
                        result['net_area'] = max(result['net_area'], val)
                    else:
                        result['gross_area'] = max(result['gross_area'], val)
                elif any(l in nl for l in ('length', 'länge')) and 'perim' not in nl:
                    result['length'] = max(result['length'], float(value) * scale_l)
                elif any(w in nl for w in ('width', 'breite')):
                    result['width'] = max(result['width'], float(value) * scale_l)
                elif any(h in nl for h in ('height', 'höhe')):
                    result['height'] = max(result['height'], float(value) * scale_l)
                elif any(t in nl for t in ('thickness', 'thick', 'depth', 'dicke')) and 'perim' not in nl:
                    result['thickness'] = max(result['thickness'], float(value) * scale_l)
                elif 'perim' in nl:
                    result['perimeter'] = max(result['perimeter'], float(value) * scale_l)

        # Apply min(Net, Gross) rule — see _extract_from_ifc_quantities for rationale.
        # Any "generic" volume captured in result['volume'] (no net/gross marker)
        # is treated as net by convention.
        n = max(result['net_volume'], result['volume'])
        g = result['gross_volume']
        if n > 0 and g > 0:
            if n <= g:
                result['volume'] = n
                result['used_net'] = True
            else:
                result['volume'] = g
                result['used_gross'] = True
        elif n > 0:
            result['volume'] = n
            result['used_net'] = True
        elif g > 0:
            result['volume'] = g
            result['used_gross'] = True

        if result['area'] == 0:
            result['area'] = result['net_area'] or result['gross_area']

        return result

    # ═══════════════════════════════════════════════════════════════════════
    # METHOD 4: GEOMETRY REPRESENTATION
    # ═══════════════════════════════════════════════════════════════════════

    def _extract_from_representation(self, element):
        result = {'length': 0.0, 'width': 0.0, 'height': 0.0,
                  'volume': 0.0, 'area': 0.0}
        try:
            if not hasattr(element, 'Representation') or not element.Representation:
                return result

            body_rep = None
            for rep in element.Representation.Representations:
                rid = rep.RepresentationIdentifier
                if rid in ('Body', 'SweptSolid', 'Brep', 'SolidModel', 'Tessellation'):
                    body_rep = rep
                    break
            if body_rep is None and element.Representation.Representations:
                body_rep = element.Representation.Representations[0]
            if body_rep is None:
                return result

            for item in body_rep.Items:
                self.geom_types_found[item.is_a()] += 1
                v, d = self._process_geom_item_recursive(item, depth=0)
                if v > result['volume']:
                    result['volume'] = v
                for k in ('length', 'width', 'height', 'area'):
                    if d.get(k, 0) > result.get(k, 0):
                        result[k] = d[k]
        except Exception:
            pass
        return result

    # ── Recursive geometry dispatcher ────────────────────────────────────

    def _process_geom_item_recursive(self, item, depth=0):
        if depth > 10:
            return 0.0, {}
        volume = 0.0
        dims = {'length': 0.0, 'width': 0.0, 'height': 0.0, 'area': 0.0}

        try:
            # IfcMappedItem (instances, very common in Revit exports)
            if item.is_a('IfcMappedItem'):
                try:
                    src = item.MappingSource.MappedRepresentation
                    for sub in src.Items:
                        v, d = self._process_geom_item_recursive(sub, depth + 1)
                        if v > volume:
                            volume, dims = v, d
                except Exception:
                    pass
                return volume, dims

            # IfcBooleanResult / IfcBooleanClippingResult (CSG operations)
            if item.is_a('IfcBooleanResult') or item.is_a('IfcBooleanClippingResult'):
                try:
                    volume, dims = self._process_geom_item_recursive(
                        item.FirstOperand, depth + 1)
                    second = item.SecondOperand
                    if second and not second.is_a('IfcHalfSpaceSolid') and \
                       not second.is_a('IfcPolygonalBoundedHalfSpace'):
                        sv, _ = self._process_geom_item_recursive(second, depth + 1)
                        if 0 < sv < volume:
                            volume -= sv
                except Exception:
                    pass
                return volume, dims

            # IfcShapeRepresentation (nested container)
            if item.is_a('IfcShapeRepresentation'):
                try:
                    for sub in item.Items:
                        v, d = self._process_geom_item_recursive(sub, depth + 1)
                        if v > volume:
                            volume, dims = v, d
                except Exception:
                    pass
                return volume, dims

            # Dispatch to concrete geometry handler
            volume, dims = self._process_geometry_item(item)

        except Exception:
            pass
        return volume, dims

    # ── Concrete geometry item processing ────────────────────────────────

    def _process_geometry_item(self, item):
        volume = 0.0
        dims = {'length': 0.0, 'width': 0.0, 'height': 0.0, 'area': 0.0}

        try:
            # ── IfcBoundingBox ────────────────────────────────────────
            if item.is_a('IfcBoundingBox'):
                dims['length'] = item.XDim * self.unit_scale_length
                dims['width'] = item.YDim * self.unit_scale_length
                dims['height'] = item.ZDim * self.unit_scale_length
                volume = dims['length'] * dims['width'] * dims['height']

            # ── IfcExtrudedAreaSolid (most common) ────────────────────
            elif item.is_a('IfcExtrudedAreaSolid'):
                dep = (item.Depth or 0) * self.unit_scale_length
                profile = item.SweptArea
                profile_area = self._calculate_profile_area(profile)
                dims['area'] = profile_area

                if profile_area > 0 and dep > 0:
                    volume = profile_area * dep

                pdims = self._get_profile_dimensions(profile)

                # Check extrusion direction
                is_vert = True
                if item.ExtrudedDirection:
                    dr = item.ExtrudedDirection.DirectionRatios
                    if len(dr) >= 3:
                        z = abs(dr[2]) if len(dr) > 2 else 0
                        xy = max(abs(dr[0]), abs(dr[1]) if len(dr) > 1 else 0)
                        if z < xy:
                            is_vert = False

                if is_vert:
                    dims['height'] = dep
                    dims['length'] = pdims.get('length', 0)
                    dims['width'] = pdims.get('width', 0)
                else:
                    dims['length'] = dep
                    dims['width'] = pdims.get('length', 0)
                    dims['height'] = pdims.get('width', 0)

            # ── IfcRevolvedAreaSolid ──────────────────────────────────
            elif item.is_a('IfcRevolvedAreaSolid'):
                try:
                    profile = item.SweptArea
                    angle = item.Angle if hasattr(item, 'Angle') else 360
                    profile_area = self._calculate_profile_area(profile)
                    if profile_area > 0:
                        pdims = self._get_profile_dimensions(profile)
                        r = max(pdims.get('length', 0), pdims.get('width', 0)) / 2
                        if r > 0:
                            volume = 2 * math.pi * r * profile_area * (angle / 360)
                except Exception:
                    pass

            # ── IfcFacetedBrep ────────────────────────────────────────
            elif item.is_a('IfcFacetedBrep'):
                try:
                    shell = item.Outer
                    if shell and shell.CfsFaces:
                        volume = self._brep_volume(shell)
                        dims = self._brep_bounds(shell)
                except Exception:
                    pass

            # ── IfcAdvancedBrep (IFC4) ────────────────────────────────
            elif item.is_a('IfcAdvancedBrep'):
                try:
                    shell = item.Outer
                    if shell and shell.CfsFaces:
                        volume = self._brep_volume(shell)
                        dims = self._brep_bounds(shell)
                except Exception:
                    pass

            # ── IfcSweptDiskSolid (pipes, cables) ─────────────────────
            elif item.is_a('IfcSweptDiskSolid'):
                try:
                    radius = item.Radius * self.unit_scale_length if item.Radius else 0
                    curve_length = self._get_curve_length(item.Directrix)
                    if radius > 0 and curve_length > 0:
                        volume = math.pi * radius * radius * curve_length
                        dims = {'length': curve_length, 'width': radius * 2,
                                'height': radius * 2, 'area': 0.0}
                except Exception:
                    pass

            # ── IfcBlock ──────────────────────────────────────────────
            elif item.is_a('IfcBlock'):
                dims['length'] = item.XLength * self.unit_scale_length
                dims['width'] = item.YLength * self.unit_scale_length
                dims['height'] = item.ZLength * self.unit_scale_length
                volume = dims['length'] * dims['width'] * dims['height']

            # ── IfcRectangularPyramid ─────────────────────────────────
            elif item.is_a('IfcRectangularPyramid'):
                x = item.XLength * self.unit_scale_length
                y = item.YLength * self.unit_scale_length
                h = item.Height * self.unit_scale_length
                dims = {'length': x, 'width': y, 'height': h, 'area': 0.0}
                volume = (x * y * h) / 3

            # ── IfcSphere ─────────────────────────────────────────────
            elif item.is_a('IfcSphere'):
                r = item.Radius * self.unit_scale_length
                dims = {'length': r * 2, 'width': r * 2, 'height': r * 2, 'area': 0.0}
                volume = (4 / 3) * math.pi * r ** 3

            # ── IfcRightCircularCone ──────────────────────────────────
            elif item.is_a('IfcRightCircularCone'):
                r = item.BottomRadius * self.unit_scale_length
                h = item.Height * self.unit_scale_length
                dims = {'length': r * 2, 'width': r * 2, 'height': h, 'area': 0.0}
                volume = (1 / 3) * math.pi * r ** 2 * h

            # ── IfcRightCircularCylinder ──────────────────────────────
            elif item.is_a('IfcRightCircularCylinder'):
                r = item.Radius * self.unit_scale_length
                h = item.Height * self.unit_scale_length
                dims = {'length': r * 2, 'width': r * 2, 'height': h, 'area': 0.0}
                volume = math.pi * r ** 2 * h

            # ── IfcPolygonalFaceSet (IFC4) ────────────────────────────
            elif item.is_a('IfcPolygonalFaceSet'):
                try:
                    coords = item.Coordinates.CoordList
                    volume, dims = self._polygonal_face_set_volume(item, coords)
                except Exception:
                    pass

            # ── IfcTriangulatedFaceSet (IFC4) ─────────────────────────
            elif item.is_a('IfcTriangulatedFaceSet'):
                try:
                    coords = item.Coordinates.CoordList
                    volume, dims = self._triangulated_face_set_volume(item, coords)
                except Exception:
                    pass

            # ── IfcShellBasedSurfaceModel ─────────────────────────────
            elif item.is_a('IfcShellBasedSurfaceModel'):
                try:
                    for shell in item.SbsmBoundary:
                        if shell.is_a('IfcClosedShell'):
                            vol = self._brep_volume(shell)
                            if vol > volume:
                                volume = vol
                                dims = self._brep_bounds(shell)
                except Exception:
                    pass

            # ── IfcSectionedSolidHorizontal (infrastructure) ──────────
            elif item.is_a('IfcSectionedSolidHorizontal'):
                try:
                    curve_length = 0.0
                    if item.Directrix:
                        curve_length = self._get_curve_length(item.Directrix)
                    avg_area = 0.0
                    if item.CrossSections:
                        total_a = sum(self._calculate_profile_area(s)
                                      for s in item.CrossSections)
                        count = len(item.CrossSections)
                        if count > 0:
                            avg_area = total_a / count
                    if curve_length > 0 and avg_area > 0:
                        volume = curve_length * avg_area
                        dims = {'length': curve_length, 'width': 0, 'height': 0, 'area': avg_area}
                except Exception:
                    pass

            # ── IfcSurfaceCurveSweptAreaSolid ─────────────────────────
            elif item.is_a('IfcSurfaceCurveSweptAreaSolid'):
                try:
                    profile = item.SweptArea
                    profile_area = self._calculate_profile_area(profile)
                    curve_length = self._get_curve_length(item.Directrix) if item.Directrix else 0
                    if profile_area > 0 and curve_length > 0:
                        volume = profile_area * curve_length
                        dims = self._get_profile_dimensions(profile)
                        dims['length'] = curve_length
                except Exception:
                    pass

            # ── IfcFixedReferenceSweptAreaSolid (IFC4) ────────────────
            elif item.is_a('IfcFixedReferenceSweptAreaSolid'):
                try:
                    profile = item.SweptArea
                    profile_area = self._calculate_profile_area(profile)
                    curve_length = self._get_curve_length(item.Directrix) if item.Directrix else 0
                    if profile_area > 0 and curve_length > 0:
                        volume = profile_area * curve_length
                        dims = self._get_profile_dimensions(profile)
                        dims['length'] = curve_length
                except Exception:
                    pass

        except Exception:
            pass
        return volume, dims

    # ═══════════════════════════════════════════════════════════════════════
    # PROFILE AREA CALCULATIONS (15 types)
    # ═══════════════════════════════════════════════════════════════════════

    def _calculate_profile_area(self, profile):
        area = 0.0
        try:
            if profile.is_a('IfcRectangleHollowProfileDef'):
                x = profile.XDim * self.unit_scale_length
                y = profile.YDim * self.unit_scale_length
                t = profile.WallThickness * self.unit_scale_length if profile.WallThickness else 0
                area = x * y - (x - 2 * t) * (y - 2 * t)

            elif profile.is_a('IfcRectangleProfileDef'):
                area = (profile.XDim * self.unit_scale_length) * \
                       (profile.YDim * self.unit_scale_length)

            elif profile.is_a('IfcCircleHollowProfileDef'):
                r_out = profile.Radius * self.unit_scale_length
                t = profile.WallThickness * self.unit_scale_length if profile.WallThickness else 0
                r_in = r_out - t
                area = math.pi * (r_out ** 2 - r_in ** 2)

            elif profile.is_a('IfcCircleProfileDef'):
                r = profile.Radius * self.unit_scale_length
                area = math.pi * r * r

            elif profile.is_a('IfcEllipseProfileDef'):
                a = profile.SemiAxis1 * self.unit_scale_length
                b = profile.SemiAxis2 * self.unit_scale_length
                area = math.pi * a * b

            elif profile.is_a('IfcAsymmetricIShapeProfileDef'):
                d = profile.OverallDepth * self.unit_scale_length
                tw = profile.WebThickness * self.unit_scale_length
                top_w = profile.TopFlangeWidth * self.unit_scale_length if profile.TopFlangeWidth else d * 0.3
                top_t = profile.TopFlangeThickness * self.unit_scale_length if profile.TopFlangeThickness else d * 0.1
                bot_w = profile.BottomFlangeWidth * self.unit_scale_length
                bot_t = getattr(profile, 'BottomFlangeThickness', None)
                bot_t = bot_t * self.unit_scale_length if bot_t else top_t
                web_h = d - top_t - bot_t
                area = top_w * top_t + bot_w * bot_t + web_h * tw

            elif profile.is_a('IfcIShapeProfileDef'):
                w = profile.OverallWidth * self.unit_scale_length
                h = profile.OverallDepth * self.unit_scale_length
                tw = profile.WebThickness * self.unit_scale_length if profile.WebThickness else w * 0.1
                tf = profile.FlangeThickness * self.unit_scale_length if profile.FlangeThickness else h * 0.1
                area = 2 * (w * tf) + (h - 2 * tf) * tw

            elif profile.is_a('IfcLShapeProfileDef'):
                d = profile.Depth * self.unit_scale_length
                w = (profile.Width if profile.Width else profile.Depth) * self.unit_scale_length
                t = profile.Thickness * self.unit_scale_length if profile.Thickness else d * 0.1
                area = d * t + (w - t) * t

            elif profile.is_a('IfcTShapeProfileDef'):
                d = profile.Depth * self.unit_scale_length
                fw = profile.FlangeWidth * self.unit_scale_length
                tw = profile.WebThickness * self.unit_scale_length
                tf = profile.FlangeThickness * self.unit_scale_length
                area = fw * tf + (d - tf) * tw

            elif profile.is_a('IfcCShapeProfileDef'):
                d = profile.Depth * self.unit_scale_length
                w = profile.Width * self.unit_scale_length
                t = profile.WallThickness * self.unit_scale_length
                girth = (profile.Girth * self.unit_scale_length
                         if hasattr(profile, 'Girth') and profile.Girth else w * 0.5)
                area = d * t + 2 * girth * t

            elif profile.is_a('IfcUShapeProfileDef'):
                d = profile.Depth * self.unit_scale_length
                w = profile.FlangeWidth * self.unit_scale_length
                tw = profile.WebThickness * self.unit_scale_length
                tf = profile.FlangeThickness * self.unit_scale_length
                area = 2 * w * tf + (d - 2 * tf) * tw

            elif profile.is_a('IfcZShapeProfileDef'):
                d = profile.Depth * self.unit_scale_length
                fw = profile.FlangeWidth * self.unit_scale_length
                tw = profile.WebThickness * self.unit_scale_length
                tf = profile.FlangeThickness * self.unit_scale_length
                area = 2 * fw * tf + (d - 2 * tf) * tw

            elif profile.is_a('IfcTrapeziumProfileDef'):
                bot = profile.BottomXDim * self.unit_scale_length
                top = profile.TopXDim * self.unit_scale_length
                h = profile.YDim * self.unit_scale_length
                area = (bot + top) * h / 2

            elif profile.is_a('IfcArbitraryProfileDefWithVoids'):
                outer = self._calculate_curve_area(profile.OuterCurve)
                if outer == 0:
                    bbox = self._get_curve_bounding_box(profile.OuterCurve)
                    if bbox['length'] > 0 and bbox['width'] > 0:
                        outer = bbox['length'] * bbox['width'] * 0.85
                void_area = 0
                if profile.InnerCurves:
                    for inner in profile.InnerCurves:
                        void_area += self._calculate_curve_area(inner)
                area = outer - void_area

            elif profile.is_a('IfcArbitraryClosedProfileDef'):
                area = self._calculate_curve_area(profile.OuterCurve)
                if area == 0:
                    bbox = self._get_curve_bounding_box(profile.OuterCurve)
                    if bbox['length'] > 0 and bbox['width'] > 0:
                        area = bbox['length'] * bbox['width'] * 0.85

        except Exception:
            pass
        return area

    # ═══════════════════════════════════════════════════════════════════════
    # PROFILE DIMENSIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _get_profile_dimensions(self, profile):
        dims = {'length': 0.0, 'width': 0.0}
        try:
            if profile.is_a('IfcRectangleProfileDef'):
                dims['length'] = profile.XDim * self.unit_scale_length
                dims['width'] = profile.YDim * self.unit_scale_length
            elif profile.is_a('IfcCircleProfileDef'):
                d = profile.Radius * 2 * self.unit_scale_length
                dims['length'] = dims['width'] = d
            elif profile.is_a('IfcIShapeProfileDef'):
                dims['length'] = profile.OverallWidth * self.unit_scale_length
                dims['width'] = profile.OverallDepth * self.unit_scale_length
            elif profile.is_a('IfcLShapeProfileDef'):
                dims['length'] = profile.Depth * self.unit_scale_length
                dims['width'] = (profile.Width if profile.Width else profile.Depth) * self.unit_scale_length
            elif profile.is_a('IfcTShapeProfileDef'):
                dims['length'] = profile.FlangeWidth * self.unit_scale_length
                dims['width'] = profile.Depth * self.unit_scale_length
            elif profile.is_a('IfcUShapeProfileDef'):
                dims['length'] = profile.FlangeWidth * self.unit_scale_length
                dims['width'] = profile.Depth * self.unit_scale_length
            elif profile.is_a('IfcZShapeProfileDef'):
                dims['length'] = profile.FlangeWidth * self.unit_scale_length
                dims['width'] = profile.Depth * self.unit_scale_length
            elif profile.is_a('IfcAsymmetricIShapeProfileDef'):
                dims['length'] = max(
                    profile.BottomFlangeWidth * self.unit_scale_length if profile.BottomFlangeWidth else 0,
                    profile.TopFlangeWidth * self.unit_scale_length if profile.TopFlangeWidth else 0)
                dims['width'] = profile.OverallDepth * self.unit_scale_length
            elif profile.is_a('IfcTrapeziumProfileDef'):
                dims['length'] = max(profile.BottomXDim, profile.TopXDim) * self.unit_scale_length
                dims['width'] = profile.YDim * self.unit_scale_length
            elif profile.is_a('IfcArbitraryClosedProfileDef') or \
                 profile.is_a('IfcArbitraryProfileDefWithVoids'):
                if hasattr(profile, 'OuterCurve'):
                    bbox = self._get_curve_bounding_box(profile.OuterCurve)
                    dims['length'] = bbox.get('length', 0)
                    dims['width'] = bbox.get('width', 0)
            else:
                if hasattr(profile, 'XDim') and profile.XDim:
                    dims['length'] = profile.XDim * self.unit_scale_length
                if hasattr(profile, 'YDim') and profile.YDim:
                    dims['width'] = profile.YDim * self.unit_scale_length
        except Exception:
            pass
        return dims

    # ═══════════════════════════════════════════════════════════════════════
    # CURVE HELPERS (7 curve types)
    # ═══════════════════════════════════════════════════════════════════════

    def _calculate_curve_area(self, curve):
        """Calculate area enclosed by curve using shoelace formula."""
        try:
            points = self._get_curve_points(curve)
            if len(points) >= 3:
                n = len(points)
                area = 0.0
                for i in range(n):
                    j = (i + 1) % n
                    area += points[i][0] * points[j][1]
                    area -= points[j][0] * points[i][1]
                return abs(area) / 2.0
        except Exception:
            pass
        return 0.0

    def _get_curve_bounding_box(self, curve):
        """Get bounding box dimensions of a curve."""
        try:
            points = self._get_curve_points(curve)
            if points:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                return {'length': max(xs) - min(xs), 'width': max(ys) - min(ys)}
        except Exception:
            pass
        return {'length': 0.0, 'width': 0.0}

    def _get_curve_points(self, curve):
        """Extract 2D points from any supported curve type."""
        points = []
        try:
            # IfcPolyline
            if curve.is_a('IfcPolyline'):
                for pt in curve.Points:
                    c = pt.Coordinates
                    points.append((c[0] * self.unit_scale_length,
                                   c[1] * self.unit_scale_length if len(c) > 1 else 0))

            # IfcIndexedPolyCurve (IFC4)
            elif curve.is_a('IfcIndexedPolyCurve'):
                if curve.Points:
                    coord_list = list(curve.Points.CoordList)
                    if curve.Segments:
                        for seg in curve.Segments:
                            try:
                                indices = list(seg)
                                if len(indices) == 3:
                                    # Arc segment
                                    arc_pts = self._approximate_arc_from_3_points(
                                        coord_list, indices, num_segments=8)
                                    for pt in arc_pts:
                                        if not points or points[-1] != pt:
                                            points.append(pt)
                                else:
                                    for idx in indices:
                                        if 1 <= idx <= len(coord_list):
                                            c = coord_list[idx - 1]
                                            pt = (c[0] * self.unit_scale_length,
                                                  c[1] * self.unit_scale_length if len(c) > 1 else 0)
                                            if not points or points[-1] != pt:
                                                points.append(pt)
                            except (TypeError, IndexError):
                                pass
                    else:
                        for c in coord_list:
                            points.append((c[0] * self.unit_scale_length,
                                           c[1] * self.unit_scale_length if len(c) > 1 else 0))

            # IfcCompositeCurve
            elif curve.is_a('IfcCompositeCurve'):
                for segment in curve.Segments:
                    parent_curve = getattr(segment, 'ParentCurve',
                                           getattr(segment, 'Curve', None))
                    same_sense = getattr(segment, 'SameSense', True)
                    if parent_curve:
                        seg_pts = self._get_curve_points(parent_curve)
                        if not same_sense:
                            seg_pts = list(reversed(seg_pts))
                        for pt in seg_pts:
                            if not points or self._pts_dist(points[-1], pt) > 1e-6:
                                points.append(pt)

            # IfcBSplineCurve / IfcBSplineCurveWithKnots
            elif curve.is_a('IfcBSplineCurve') or curve.is_a('IfcBSplineCurveWithKnots'):
                try:
                    if hasattr(curve, 'ControlPointsList') and curve.ControlPointsList:
                        for pt in curve.ControlPointsList:
                            if hasattr(pt, 'Coordinates'):
                                c = list(pt.Coordinates)
                                points.append((c[0] * self.unit_scale_length,
                                               c[1] * self.unit_scale_length if len(c) > 1 else 0))
                except Exception:
                    pass

            # IfcLine
            elif curve.is_a('IfcLine'):
                try:
                    if curve.Pnt:
                        c = list(curve.Pnt.Coordinates)
                        x = c[0] * self.unit_scale_length
                        y = c[1] * self.unit_scale_length if len(c) > 1 else 0
                        points.append((x, y))
                        if curve.Dir and hasattr(curve.Dir, 'Magnitude') and hasattr(curve.Dir, 'Orientation'):
                            mag = curve.Dir.Magnitude * self.unit_scale_length
                            if curve.Dir.Orientation:
                                dr = list(curve.Dir.Orientation.DirectionRatios)
                                points.append((x + mag * dr[0],
                                               y + mag * dr[1] if len(dr) > 1 else y))
                except Exception:
                    pass

            # IfcTrimmedCurve (often arcs)
            elif curve.is_a('IfcTrimmedCurve'):
                basis = curve.BasisCurve
                if basis.is_a('IfcLine'):
                    for trim in [curve.Trim1, curve.Trim2]:
                        for t in trim:
                            if hasattr(t, 'Coordinates'):
                                c = list(t.Coordinates)
                                points.append((c[0] * self.unit_scale_length,
                                               c[1] * self.unit_scale_length if len(c) > 1 else 0))
                elif basis.is_a('IfcCircle'):
                    points.extend(self._approximate_trimmed_arc(curve))

            # IfcCircle (full circle)
            elif curve.is_a('IfcCircle'):
                r = curve.Radius * self.unit_scale_length
                center = [0, 0]
                if curve.Position and curve.Position.Location:
                    center = [c * self.unit_scale_length
                              for c in curve.Position.Location.Coordinates[:2]]
                for i in range(32):
                    angle = 2 * math.pi * i / 32
                    points.append((center[0] + r * math.cos(angle),
                                   center[1] + r * math.sin(angle)))

        except Exception:
            pass
        return points

    def _approximate_arc_from_3_points(self, coord_list, indices, num_segments=8):
        """Approximate arc from 3 points (start, mid, end)."""
        points = []
        try:
            p1 = coord_list[indices[0] - 1]
            p2 = coord_list[indices[1] - 1]
            p3 = coord_list[indices[2] - 1]

            x1, y1 = p1[0] * self.unit_scale_length, (p1[1] * self.unit_scale_length if len(p1) > 1 else 0)
            x2, y2 = p2[0] * self.unit_scale_length, (p2[1] * self.unit_scale_length if len(p2) > 1 else 0)
            x3, y3 = p3[0] * self.unit_scale_length, (p3[1] * self.unit_scale_length if len(p3) > 1 else 0)

            # Circumcircle from 3 points
            d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
            if abs(d) < 1e-10:
                return [(x1, y1), (x2, y2), (x3, y3)]

            cx = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) +
                  (x3**2 + y3**2) * (y1 - y2)) / d
            cy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) +
                  (x3**2 + y3**2) * (x2 - x1)) / d
            radius = math.sqrt((x1 - cx) ** 2 + (y1 - cy) ** 2)

            a1 = math.atan2(y1 - cy, x1 - cx)
            a2 = math.atan2(y2 - cy, x2 - cx)
            a3 = math.atan2(y3 - cy, x3 - cx)

            def norm(a):
                while a < 0: a += 2 * math.pi
                while a >= 2 * math.pi: a -= 2 * math.pi
                return a

            a1, a2, a3 = norm(a1), norm(a2), norm(a3)

            def between(a, b, c):
                return (a <= c <= b) if a <= b else (c >= a or c <= b)

            if between(a1, a3, a2):
                if a3 < a1:
                    a3 += 2 * math.pi
            else:
                if a1 < a3:
                    a1 += 2 * math.pi
                a1, a3 = a3, a1

            for i in range(num_segments + 1):
                t = i / num_segments
                angle = a1 + t * (a3 - a1)
                points.append((cx + radius * math.cos(angle),
                                cy + radius * math.sin(angle)))
        except Exception:
            try:
                for idx in indices:
                    if 1 <= idx <= len(coord_list):
                        c = coord_list[idx - 1]
                        points.append((c[0] * self.unit_scale_length,
                                       c[1] * self.unit_scale_length if len(c) > 1 else 0))
            except Exception:
                pass
        return points

    def _approximate_trimmed_arc(self, trimmed_curve, num_segments=16):
        """Approximate a trimmed arc with multiple points."""
        points = []
        try:
            basis = trimmed_curve.BasisCurve
            if not basis.is_a('IfcCircle'):
                return points

            radius = basis.Radius * self.unit_scale_length
            center = [0.0, 0.0]
            if basis.Position and basis.Position.Location:
                center = [c * self.unit_scale_length
                          for c in basis.Position.Location.Coordinates[:2]]

            start_angle = end_angle = None
            for t in trimmed_curve.Trim1:
                if isinstance(t, float):
                    start_angle = t
                elif hasattr(t, 'wrappedValue'):
                    start_angle = t.wrappedValue
            for t in trimmed_curve.Trim2:
                if isinstance(t, float):
                    end_angle = t
                elif hasattr(t, 'wrappedValue'):
                    end_angle = t.wrappedValue

            if start_angle is not None and end_angle is not None:
                if abs(start_angle) > 2 * math.pi or abs(end_angle) > 2 * math.pi:
                    start_angle = math.radians(start_angle)
                    end_angle = math.radians(end_angle)
                if trimmed_curve.SenseAgreement:
                    if end_angle < start_angle:
                        end_angle += 2 * math.pi
                else:
                    if start_angle < end_angle:
                        start_angle += 2 * math.pi
                    start_angle, end_angle = end_angle, start_angle
                for i in range(num_segments + 1):
                    t = i / num_segments
                    angle = start_angle + t * (end_angle - start_angle)
                    points.append((center[0] + radius * math.cos(angle),
                                   center[1] + radius * math.sin(angle)))
            else:
                for trim in [trimmed_curve.Trim1, trimmed_curve.Trim2]:
                    for t in trim:
                        if hasattr(t, 'Coordinates'):
                            c = list(t.Coordinates)
                            points.append((c[0] * self.unit_scale_length,
                                           c[1] * self.unit_scale_length if len(c) > 1 else 0))
        except Exception:
            pass
        return points

    @staticmethod
    def _pts_dist(p1, p2):
        try:
            return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
        except Exception:
            return float('inf')

    # ═══════════════════════════════════════════════════════════════════════
    # BREP / FACE SET VOLUME CALCULATIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _brep_volume(self, shell):
        """Calculate BRep volume using signed tetrahedron method."""
        vol = 0.0
        try:
            for face in shell.CfsFaces:
                for bound in face.Bounds:
                    loop = bound.Bound
                    if loop.is_a('IfcPolyLoop'):
                        pts = [(p.Coordinates[0] * self.unit_scale_length,
                                p.Coordinates[1] * self.unit_scale_length,
                                p.Coordinates[2] * self.unit_scale_length)
                               for p in loop.Polygon]
                        if len(pts) >= 3:
                            for k in range(1, len(pts) - 1):
                                v0, v1, v2 = pts[0], pts[k], pts[k + 1]
                                vol += (v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
                                        - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
                                        + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2]))
        except Exception:
            pass
        return abs(vol) / 6.0

    def _brep_bounds(self, shell):
        """Calculate bounding box of BRep shell."""
        min_x = min_y = min_z = float('inf')
        max_x = max_y = max_z = float('-inf')
        try:
            for face in shell.CfsFaces:
                for bound in face.Bounds:
                    loop = bound.Bound
                    if loop.is_a('IfcPolyLoop'):
                        for p in loop.Polygon:
                            c = [co * self.unit_scale_length for co in p.Coordinates]
                            min_x, max_x = min(min_x, c[0]), max(max_x, c[0])
                            if len(c) > 1:
                                min_y, max_y = min(min_y, c[1]), max(max_y, c[1])
                            if len(c) > 2:
                                min_z, max_z = min(min_z, c[2]), max(max_z, c[2])
        except Exception:
            pass
        if min_x < float('inf'):
            return {'length': max_x - min_x,
                    'width': max_y - min_y if min_y < float('inf') else 0,
                    'height': max_z - min_z if min_z < float('inf') else 0,
                    'area': 0.0}
        return {'length': 0.0, 'width': 0.0, 'height': 0.0, 'area': 0.0}

    def _polygonal_face_set_volume(self, face_set, coords):
        """Calculate volume of IfcPolygonalFaceSet."""
        volume = 0.0
        dims = {'length': 0.0, 'width': 0.0, 'height': 0.0, 'area': 0.0}
        try:
            vertices = [[x * self.unit_scale_length for x in c] for c in coords]
            if vertices:
                xs = [v[0] for v in vertices]
                ys = [v[1] for v in vertices]
                zs = [v[2] for v in vertices] if len(vertices[0]) > 2 else [0]
                dims = {'length': max(xs) - min(xs), 'width': max(ys) - min(ys),
                        'height': max(zs) - min(zs), 'area': 0.0}

            if face_set.Faces:
                for face in face_set.Faces:
                    indices = list(face.CoordIndex)
                    if len(indices) >= 3:
                        v0 = vertices[indices[0] - 1]
                        for i in range(1, len(indices) - 1):
                            v1 = vertices[indices[i] - 1]
                            v2 = vertices[indices[i + 1] - 1]
                            if len(v0) >= 3 and len(v1) >= 3 and len(v2) >= 3:
                                volume += (v0[0] * (v1[1] * v2[2] - v2[1] * v1[2]) -
                                           v1[0] * (v0[1] * v2[2] - v2[1] * v0[2]) +
                                           v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])) / 6.0
            volume = abs(volume)
        except Exception:
            pass
        return volume, dims

    def _triangulated_face_set_volume(self, face_set, coords):
        """Calculate volume of IfcTriangulatedFaceSet."""
        volume = 0.0
        dims = {'length': 0.0, 'width': 0.0, 'height': 0.0, 'area': 0.0}
        try:
            vertices = [[x * self.unit_scale_length for x in c] for c in coords]
            if vertices:
                xs = [v[0] for v in vertices]
                ys = [v[1] for v in vertices]
                zs = [v[2] for v in vertices] if len(vertices[0]) > 2 else [0]
                dims = {'length': max(xs) - min(xs), 'width': max(ys) - min(ys),
                        'height': max(zs) - min(zs), 'area': 0.0}

            if face_set.CoordIndex:
                for tri in face_set.CoordIndex:
                    indices = list(tri)
                    if len(indices) >= 3:
                        v0 = vertices[indices[0] - 1]
                        v1 = vertices[indices[1] - 1]
                        v2 = vertices[indices[2] - 1]
                        if len(v0) >= 3 and len(v1) >= 3 and len(v2) >= 3:
                            volume += (v0[0] * (v1[1] * v2[2] - v2[1] * v1[2]) -
                                       v1[0] * (v0[1] * v2[2] - v2[1] * v0[2]) +
                                       v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])) / 6.0
            volume = abs(volume)
        except Exception:
            pass
        return volume, dims

    def _get_curve_length(self, curve):
        """Calculate length of a curve (used for swept solid volume calculation)."""
        length = 0.0
        try:
            if curve.is_a('IfcPolyline'):
                pts = [[c * self.unit_scale_length for c in p.Coordinates]
                       for p in curve.Points]
                for i in range(len(pts) - 1):
                    dx = pts[i + 1][0] - pts[i][0]
                    dy = pts[i + 1][1] - pts[i][1] if len(pts[i]) > 1 else 0
                    dz = pts[i + 1][2] - pts[i][2] if len(pts[i]) > 2 else 0
                    length += math.sqrt(dx * dx + dy * dy + dz * dz)
            elif curve.is_a('IfcCircle'):
                r = curve.Radius * self.unit_scale_length
                length = 2 * math.pi * r
        except Exception:
            pass
        return length

    # ═══════════════════════════════════════════════════════════════════════
    # ELEMENT PROPERTY HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _hc_void(self, thickness_mm):
        """Get hollowcore void ratio by thickness."""
        for t in sorted(HOLLOWCORE_VOID.keys()):
            if thickness_mm <= t:
                return HOLLOWCORE_VOID[t]
        return 0.50

    def _get_storey(self, element):
        try:
            c = element_util.get_container(element)
            if c and hasattr(c, 'Name'):
                return c.Name or ''
        except Exception:
            pass
        # Fallback
        try:
            if hasattr(element, 'ContainedInStructure'):
                for rel in element.ContainedInStructure:
                    struct = rel.RelatingStructure
                    if struct.is_a('IfcBuildingStorey'):
                        return struct.Name or ''
        except Exception:
            pass
        return ''

    def _get_material(self, element):
        try:
            mats = element_util.get_materials(element)
            if mats:
                return getattr(mats[0], 'Name', str(mats[0]))
        except Exception:
            pass
        return ''

    def _get_material_thickness(self, element):
        thickness = 0.0
        try:
            if hasattr(element, 'HasAssociations'):
                for assoc in element.HasAssociations:
                    if assoc.is_a('IfcRelAssociatesMaterial'):
                        mat = assoc.RelatingMaterial
                        layers = None
                        if mat.is_a('IfcMaterialLayerSetUsage'):
                            ls = mat.ForLayerSet
                            layers = ls.MaterialLayers if ls else None
                        elif mat.is_a('IfcMaterialLayerSet'):
                            layers = mat.MaterialLayers
                        if layers:
                            for layer in layers:
                                if layer.LayerThickness:
                                    thickness += layer.LayerThickness * self.unit_scale_length
        except Exception:
            pass
        return thickness

    def _get_opening_volume(self, element):
        """Extract total opening volume for walls (doors, windows)."""
        guid = getattr(element, 'GlobalId', None)
        if guid and guid in self.opening_volumes:
            return self.opening_volumes[guid]

        total = 0.0
        try:
            if hasattr(element, 'HasOpenings'):
                for rel in element.HasOpenings:
                    if rel.is_a('IfcRelVoidsElement'):
                        opening = rel.RelatedOpeningElement
                        if not opening:
                            continue
                        ov = 0.0

                        # Try Qto
                        qto = self._extract_from_ifc_quantities(opening)
                        if qto['volume'] > 0:
                            ov = qto['volume']
                        else:
                            # Try geometry
                            rep = self._extract_from_representation(opening)
                            ov = rep.get('volume', 0)

                        # Try filling dimensions (door/window)
                        if ov == 0:
                            if hasattr(opening, 'HasFillings'):
                                for fill_rel in opening.HasFillings:
                                    filling = fill_rel.RelatedBuildingElement
                                    if filling:
                                        fd = self._extract_quantities_from_psets(
                                            self._get_all_psets(filling))
                                        h, w = fd.get('height', 0), fd.get('width', 0)
                                        d = fd.get('thickness', 0) or 0.2
                                        if h > 0 and w > 0:
                                            ov = h * w * d

                        # Try opening's own psets
                        if ov == 0:
                            od = self._extract_quantities_from_psets(
                                self._get_all_psets(opening))
                            h, w = od.get('height', 0), od.get('width', 0)
                            d = od.get('thickness', 0) or od.get('length', 0) or 0.2
                            if h > 0 and w > 0:
                                ov = h * w * d

                        total += ov
        except Exception:
            pass

        if guid:
            self.opening_volumes[guid] = total
        return total

    def _get_position(self, element):
        """Extract world position of an element."""
        try:
            if hasattr(element, 'ObjectPlacement') and element.ObjectPlacement:
                return self._resolve_placement(element.ObjectPlacement)
        except Exception:
            pass
        return (0.0, 0.0, 0.0)

    def _resolve_placement(self, placement, depth=0):
        if depth > 10:
            return (0.0, 0.0, 0.0)
        x, y, z = 0.0, 0.0, 0.0
        try:
            if placement.is_a('IfcLocalPlacement'):
                rp = placement.RelativePlacement
                if rp and rp.is_a('IfcAxis2Placement3D') and rp.Location:
                    coords = rp.Location.Coordinates
                    x = coords[0] * self.unit_scale_length
                    y = coords[1] * self.unit_scale_length
                    z = coords[2] * self.unit_scale_length if len(coords) > 2 else 0
                if placement.PlacementRelTo:
                    px, py, pz = self._resolve_placement(placement.PlacementRelTo, depth + 1)
                    x += px
                    y += py
                    z += pz
        except Exception:
            pass
        return (x, y, z)

    # ═══════════════════════════════════════════════════════════════════════
    # BUILD OUTPUT DATAFRAME
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _strip_instance_id(name):
        """Strip Revit instance ID suffix from element name.

        E.g. 'Concrete-Rectangular-Column:400x400:314485' → 'Concrete-Rectangular-Column:400x400'
        The last segment after ':' is removed only if it is purely numeric (a Revit element ID).
        """
        if not name:
            return name
        # Strip trailing Revit instance ID in colon format: "Name:400x400:314485"
        if ':' in name:
            parts = name.rsplit(':', 1)
            if len(parts) == 2 and parts[1].strip().isdigit():
                name = parts[0]
        # Strip trailing Revit instance ID in bracket format: "Name [314485]"
        if name.endswith(']'):
            bracket = name.rfind(' [')
            if bracket != -1 and name[bracket+2:-1].isdigit():
                name = name[:bracket]
        return name

    def _build_dataframe(self):
        if not self.elements:
            return pd.DataFrame()

        # Strip internal-only fields before materialising the DataFrame
        for e in self.elements:
            e.pop('_bbox', None)

        df = pd.DataFrame(self.elements)

        # Filter structural elements with volume > 0
        structural_cats = {
            'Column', 'Beam', 'Slab/Floor', 'Wall',
            'Foundation/Footing', 'Pile', 'Stair', 'Roof',
            'Reinforcement', 'Post-Tensioning',
            # Structural sub-types from STRUCTURAL_OVERRIDES
            'Pile Cap', 'Raft Foundation', 'Pad Footing', 'Strip Footing',
            'Core Wall', 'Shear Wall', 'Retaining Wall', 'Basement Wall',
            'Transfer Beam', 'Ground Beam', 'Lintel',
            'Transfer Slab', 'Ground Floor Slab', 'Basement Slab',
            'Podium Slab', 'Hollowcore Slab',
        }
        mask = df['category'].isin(structural_cats) & (df['volume_m3'] > 0)
        df = df[mask].copy()

        if len(df) == 0:
            return df

        # Strip Revit instance IDs from names so identical types group together
        # e.g. "Concrete-Rectangular-Column:400x400:314485" → "Concrete-Rectangular-Column:400x400"
        df['name'] = df['name'].apply(self._strip_instance_id)

        # Round dimensions so near-identical sizes group together
        for dim in ('width', 'depth'):
            if dim in df.columns:
                df[dim] = pd.to_numeric(df[dim], errors='coerce').fillna(0).round(0)

        # Walls & foundations: group by name only (dimensions vary within the same type)
        # Other categories: group by name + dimensions
        wall_found_mask = df['category'].isin({
            'Wall', 'Foundation/Footing',
            'Core Wall', 'Shear Wall', 'Retaining Wall', 'Basement Wall',
            'Pile Cap', 'Raft Foundation', 'Pad Footing', 'Strip Footing', 'Pile',
        })
        if wall_found_mask.any():
            df.loc[wall_found_mask, 'width'] = 0
            df.loc[wall_found_mask, 'depth'] = 0

        group_cols = ['name', 'category', 'width', 'depth']
        grouped = df.groupby(group_cols, dropna=False).agg({
            'volume_m3': 'sum',
            'gross_volume_m3': 'sum',
            'net_volume_m3': 'sum',
            'void_volume_m3': 'sum',
            'opening_volume_m3': 'sum',
            'overlap_deduction_m3': 'sum',
            'area_m2': 'sum',
            'density_kg_m3': 'first',
            'weight_kg': 'sum',
            'is_steel': 'first',
            'is_precast': 'first',
            'slab_type': 'first',
            'ifc_type': 'first',
            'family': 'first',
            'material': 'first',
            'height': 'first',
            'thickness': 'first',
            'level': lambda x: ', '.join(sorted(set(str(v) for v in x if pd.notna(v)))),
            'pos_x': 'first',
            'pos_y': 'first',
            'pos_z': 'first',
            'source': 'first',
            'global_id': 'count',
        }).rename(columns={'global_id': 'count'}).reset_index()

        return grouped
