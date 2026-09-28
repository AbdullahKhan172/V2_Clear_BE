"""
Tables.
=======
Two of them, because two different consumers need the extracted data in two
different shapes:

  Run       one row per upload - status, summary, and the wizard config that
            steps 2-5 accumulate.
  Element   one row per extracted structural element, carrying exactly the 20
            display fields the UI renders. A real table (not a JSON blob)
            because Step 2 filters by category and level, paginates, and sums
            volume per category - all of which is SQL's job on 10k rows.

What is DELIBERATELY not here: the pandas DataFrame the calculation consumes.
It carries 17-28 columns (Waste, Density, is_structural_steel, has_modeled_rebar,
family, ifc_type, width/depth, weight_kg ...) that the 20-field UI contract does
not include, and those columns change which emission factor a row gets. It is
stored as dtype-exact Parquet via app/blobs.py and referenced by `frame_key`.
Reconstructing it from Element rows would silently alter the carbon numbers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    """One upload and everything derived from it."""
    __tablename__ = 'runs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # ── Ownership ───────────────────────────────────────────────────────
    # Who this run belongs to. Today that is the browser's own client id, held
    # in its localStorage; once sign-in lands it becomes the user id, and a
    # user's first login CLAIMS every run still carrying their browser's client
    # id. That claim is the whole reason this column exists now rather than
    # later - added after there is real data, it is a migration over live runs.
    #
    # NULL means "created before ownership existed". Such a run is readable by
    # anyone and is adopted by the first owner to touch it, so no existing work
    # is orphaned. New runs always carry an owner.
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                 index=True)

    # ── Upload ──────────────────────────────────────────────────────────
    filename: Mapped[str] = mapped_column(String(255))
    ext: Mapped[str] = mapped_column(String(16))
    # A storage KEY (`<run_id>/source.ifc`), not a filesystem path. It has to
    # resolve from a Celery worker that shares no disk with the web process, so
    # it must not name a location on any one machine.
    upload_key: Mapped[str] = mapped_column(Text, default='')

    # ── Lifecycle ───────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(16), default='created', index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Extraction summary (cheap fields the wizard reads constantly) ───
    source_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    csv_fmt: Mapped[str | None] = mapped_column(String(32), nullable=True)
    n_elements: Mapped[int] = mapped_column(Integer, default=0)
    has_real_eids: Mapped[bool] = mapped_column(Boolean, default=False)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    default_rates: Mapped[dict] = mapped_column(JSON, default=dict)

    # ── Blob pointers (local dir now, object storage later) ─────────────
    frame_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    geometry_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Wizard state ────────────────────────────────────────────────────
    # Project details from step 1 plus whatever steps 2-5 add. JSON here maps to
    # JSONB on Postgres: nested and schema-fluid, without losing SQL elsewhere.
    project: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # ── Calculation results (populated by the calculate job) ────────────
    # Scalars live as columns, not JSON, because these are the figures you sort
    # and compare projects by: "every run for this project by kgCO2e/m2".
    total_ton: Mapped[float | None] = mapped_column(Float, nullable=True)
    per_sqm: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    rating: Mapped[str | None] = mapped_column(String(4), nullable=True)
    rating_row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    a1_a3_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    a4_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    a5_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Biogenic carbon stored in timber. Negative, reported SEPARATELY per
    # EN 16485 - never netted into the A1-A5 total above.
    sequestration_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    a5a_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    a5a_ton: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    catalogue_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    calculated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_utcnow, onupdate=_utcnow)

    elements: Mapped[list['Element']] = relationship(
        back_populates='run', cascade='all, delete-orphan', lazy='selectin')
    result_rows: Mapped[list['ResultRow']] = relationship(
        back_populates='run', cascade='all, delete-orphan', lazy='selectin')

    def implied_max_step(self) -> int:
        """The furthest step this run has DEMONSTRABLY reached.

        Progress is also recorded explicitly, but that record only exists for
        runs created since it was added - and a value can be missing for other
        reasons too (an interrupted save, a run restored from a backup). The
        run's own lifecycle is the harder evidence:

          parsed        Step 1 is finished by definition - there are extracted
                        elements, which cannot exist without a processed upload
          calculated    every step has been through at least once

        Used as a FLOOR under the stored value, never a replacement, so a user
        who walked further than this still keeps their place.
        """
        if self.calculated_at is not None:
            return 6
        if self.status in ('parsed', 'calculating', 'complete'):
            return 2
        return 1

    def listing(self) -> dict:
        """One row of "your projects" - enough to decide what to reopen.

        Deliberately NOT the full summary: this endpoint returns every run a
        user has, and the element rows behind them are what make a run heavy.
        """
        project = self.project or {}
        wizard = (self.config or {}).get('wizard') or {}
        return {
            'run_id': self.id,
            'name': str(project.get('name') or 'Untitled'),
            'filename': self.filename,
            'ext': self.ext,
            'status': self.status,
            'error': self.error,
            'n_elements': self.n_elements,
            'area': project.get('area'),
            'stage': project.get('stage'),
            # Present only once calculated; the list shows a dash otherwise.
            'total_ton': self.total_ton,
            'per_sqm': self.per_sqm,
            'rating': self.rating,
            # How far the wizard got, so the list can say "reached Step 3 of 6"
            # and resume there. Floored by what the run has actually done, so a
            # run recorded before progress was tracked does not read as "Step 1"
            # - and, more importantly, does not open locked to Step 1.
            'max_step': max(int(wizard.get('max_step') or 1),
                            self.implied_max_step()),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def public(self) -> dict:
        """Status payload for the API."""
        return {
            'run_id': self.id,
            'filename': self.filename,
            'ext': self.ext,
            'status': self.status,
            'error': self.error,
            'n_elements': self.n_elements,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def summary(self) -> dict:
        """Everything about the extraction except the element rows themselves."""
        return {
            'run_id': self.id,
            'filename': self.filename,
            'source_type': self.source_type,
            'csv_fmt': self.csv_fmt,
            'n_elements': self.n_elements,
            'has_real_eids': self.has_real_eids,
            'categories': self.categories or [],
            'default_rates': self.default_rates or {},
            'project': self.project or {},
        }


class Element(Base):
    """One extracted structural element - the 20 fields the UI renders.

    Column names match the JSON contract exactly so the API never has to
    translate, and so a mismatch shows up as a NameError rather than a silently
    missing field.
    """
    __tablename__ = 'elements'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey('runs.id', ondelete='CASCADE'), index=True)

    # Position in the extraction. The wizard's exclusion list is positional, so
    # this must stay stable and match the DataFrame's row order.
    idx: Mapped[int] = mapped_column(Integer)

    cat: Mapped[str] = mapped_column(String(120), default='')
    name: Mapped[str] = mapped_column(Text, default='')
    level: Mapped[str] = mapped_column(String(120), default='')
    vol: Mapped[float] = mapped_column(Float, default=0.0)
    count: Mapped[int] = mapped_column(Integer, default=1)
    size: Mapped[str] = mapped_column(String(64), default='')
    is_steel: Mapped[bool] = mapped_column(Boolean, default=False)
    material: Mapped[str] = mapped_column(String(64), default='')
    e_id: Mapped[str] = mapped_column(String(32), default='')
    factor_type: Mapped[str] = mapped_column(String(32), default='')
    grade: Mapped[str] = mapped_column(String(32), default='')
    # Nullable on purpose: null means "the file did not say", which is different
    # from 0% GGBS. Collapsing the two would misreport the mix actually used.
    ggbs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mass: Mapped[float] = mapped_column(Float, default=0.0)
    default_rebar: Mapped[float] = mapped_column(Float, default=150)
    rebar_hint: Mapped[str] = mapped_column(String(64), default='')
    pt_applicable: Mapped[bool] = mapped_column(Boolean, default=False)
    kg_per_m_hint: Mapped[float] = mapped_column(Float, default=0.0)
    mass_source: Mapped[str] = mapped_column(String(16), default='')
    allow_full_factor: Mapped[bool] = mapped_column(Boolean, default=False)

    run: Mapped[Run] = relationship(back_populates='elements')

    __table_args__ = (
        # Step 2 reads elements ordered by position, and filters by category and
        # level. These are the two access patterns that matter at 10k rows.
        Index('ix_elements_run_idx', 'run_id', 'idx'),
        Index('ix_elements_run_cat', 'run_id', 'cat'),
        Index('ix_elements_run_level', 'run_id', 'level'),
    )

    # The wire contract. Kept as an explicit tuple so adding a column without
    # deciding whether the UI should see it is impossible.
    FIELDS = ('idx', 'cat', 'name', 'level', 'vol', 'count', 'size', 'is_steel',
              'material', 'e_id', 'factor_type', 'grade', 'ggbs', 'mass',
              'default_rebar', 'rebar_hint', 'pt_applicable', 'kg_per_m_hint',
              'mass_source', 'allow_full_factor')

    def public(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}


class ResultRow(Base):
    """One priced BOQ line - the atom every dashboard chart aggregates.

    A real table rather than a JSON blob because that is exactly what the charts
    need: material split is GROUP BY material, the stage split is SUM of the
    three stage columns, the category hotspot is GROUP BY category. Storing
    chart-shaped data instead would mean re-running the calculation (nine engine
    passes with sensitivity on) every time a chart changed.

    Mirrors calculations.prepare_detailed_data() field for field.
    """
    __tablename__ = 'result_rows'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey('runs.id', ondelete='CASCADE'), index=True)
    idx: Mapped[int] = mapped_column(Integer)

    e_id: Mapped[str] = mapped_column(String(32), default='')
    material: Mapped[str] = mapped_column(String(32), default='', index=True)
    category: Mapped[str] = mapped_column(String(120), default='')
    description: Mapped[str] = mapped_column(Text, default='')
    family: Mapped[str] = mapped_column(String(160), default='')
    ifc_type: Mapped[str] = mapped_column(String(64), default='')
    level: Mapped[str] = mapped_column(String(120), default='')
    count: Mapped[int] = mapped_column(Integer, default=1)
    mass: Mapped[float] = mapped_column(Float, default=0.0)
    # Net volume in m3, carried from the calculation rather than re-derived from
    # mass: dividing by an assumed density is wrong for timber, blockwork and
    # anything else that is not 2400 or 7850, and the 3D viewer colours elements
    # by kgCO2e per m3.
    volume: Mapped[float] = mapped_column(Float, default=0.0)

    # All in kgCO2e. Stored per stage rather than only as a total so the stage
    # chart is a sum rather than a re-derivation.
    a1_a3: Mapped[float] = mapped_column(Float, default=0.0)
    a4: Mapped[float] = mapped_column(Float, default=0.0)
    a5: Mapped[float] = mapped_column(Float, default=0.0)
    total_kg: Mapped[float] = mapped_column(Float, default=0.0)
    # Negative, timber only. Kept in its own column and never added into the
    # totals above - EN 16485 requires biogenic carbon reported separately.
    sequestration: Mapped[float] = mapped_column(Float, default=0.0)

    run: Mapped[Run] = relationship(back_populates='result_rows')

    __table_args__ = (
        Index('ix_result_run_material', 'run_id', 'material'),
        Index('ix_result_run_category', 'run_id', 'category'),
        Index('ix_result_run_idx', 'run_id', 'idx'),
    )

    FIELDS = ('idx', 'e_id', 'material', 'category', 'description', 'family',
              'ifc_type', 'level', 'count', 'mass', 'volume', 'a1_a3', 'a4',
              'a5', 'total_kg', 'sequestration')

    def public(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}
