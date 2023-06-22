from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from backend.business.kpis.kpi import Kpi
from backend.business.kpis.value import Value
from backend.business.period import Period
from backend.db.persister import Persister


@dataclass
class Timestamps:
    start: int
    stop: int


class Processor:
    """A processor is responsible for transforming indicator records into kpis"""

    def __init__(self, now: Period) -> None:
        self.kpis: List[Kpi] = []
        self.current_period = now
        self.current_day = now.get_truncated_value("D")

    def process_tick(self, now: Period, session: Session) -> bool:
        """Process a clock tick

        Returns True if KPIs have been updated"""

        # if we are in the same period, nothing to do
        if self.current_period == now:
            return False

        period_to_compute = self.current_period
        self.current_period = now
        # create/update KPIs values for every kind of aggregation period
        # that are update hourly
        for kpi in self.kpis:
            for agg_kind in ["D", "W", "M"]:
                Processor.compute_kpi_values_for_aggregation_kind(
                    now=period_to_compute, kpi=kpi, agg_kind=agg_kind, session=session
                )

        # create/update KPIs values for yearly aggregation period
        # which are updated only once per day
        now_day = now.get_truncated_value("D")
        if self.current_day != now_day:
            for kpi in self.kpis:
                Processor.compute_kpi_values_for_aggregation_kind(
                    now=period_to_compute, kpi=kpi, agg_kind="Y", session=session
                )
            self.current_day = now_day

        return True

    @classmethod
    def get_aggregations_to_keep(cls, agg_kind: str, now: Period) -> List[str] | None:
        if agg_kind == "D":
            return [
                now.get_shifted(relativedelta(days=-delta)).get_truncated_value(
                    agg_kind
                )
                for delta in range(0, 7)
            ]
        if agg_kind == "W":
            return [
                now.get_shifted(relativedelta(weeks=-delta)).get_truncated_value(
                    agg_kind
                )
                for delta in range(0, 4)
            ]
        if agg_kind == "M":
            return [
                now.get_shifted(relativedelta(months=-delta)).get_truncated_value(
                    agg_kind
                )
                for delta in range(0, 12)
            ]
        if agg_kind == "Y":
            return None  # Special value meaning that all values are kept
        raise AttributeError

    @classmethod
    def compute_kpi_values_for_aggregation_kind(
        cls, now: Period, kpi: Kpi, agg_kind: str, session: Session
    ) -> None:
        """Compute KPI values and update DB accordingly

        This method act on a given KPI for a given kind of aggregation.
        Existing KPI values are updated in DB. New ones are created.
        Old ones are deleted"""
        values: List[Value] = Persister.get_kpi_values(
            kpi_id=kpi.unique_id, agg_kind=agg_kind, session=session
        )
        current_agg_value = now.get_truncated_value(agg_kind)
        aggregations_to_keep = Processor.get_aggregations_to_keep(agg_kind, now)
        value_updated = False
        timestamps = cls.get_timestamps(agg_kind=agg_kind, now=now)
        for value in values:
            if aggregations_to_keep and value.agg_value not in aggregations_to_keep:
                # delete old KPI values
                Persister.delete_kpi_value(
                    kpi_id=kpi.unique_id,
                    agg_kind=agg_kind,
                    agg_value=value.agg_value,
                    session=session,
                )
            if value.agg_value == current_agg_value:
                # update the existing KPI value
                value_updated = True
                value.kpi_value = kpi.get_value(
                    agg_kind=agg_kind,
                    start_ts=timestamps.start,
                    stop_ts=timestamps.stop,
                    session=session,
                )
                Persister.update_kpi_value(
                    kpi_id=kpi.unique_id,
                    agg_kind=agg_kind,
                    agg_value=value.agg_value,
                    kpi_value=value.kpi_value,
                    session=session,
                )
        if not value_updated:
            # create a new KPI value since there is no existing one
            value = Value(
                agg_value=current_agg_value,
                kpi_value=kpi.get_value(
                    agg_kind=agg_kind,
                    start_ts=timestamps.start,
                    stop_ts=timestamps.stop,
                    session=session,
                ),
            )
            Persister.add_kpi_value(
                kpi_id=kpi.unique_id,
                agg_kind=agg_kind,
                agg_value=value.agg_value,
                kpi_value=value.kpi_value,
                session=session,
            )

    @classmethod
    def get_timestamps(cls, agg_kind: str, now: Period) -> Timestamps:
        if agg_kind == "D":
            start = datetime(year=now.year, month=now.month, day=now.day)
            return Timestamps(
                start=int(start.timestamp()),
                stop=int((start + timedelta(days=1)).timestamp()),
            )
        if agg_kind == "W":
            start = datetime(year=now.year, month=now.month, day=now.day) + timedelta(
                days=1 - now.weekday
            )
            return Timestamps(
                start=int(start.timestamp()),
                stop=int((start + timedelta(days=7)).timestamp()),
            )
        if agg_kind == "M":
            start = datetime(year=now.year, month=now.month, day=1)
            stop = datetime(year=now.year, month=now.month + 1, day=1)
            return Timestamps(
                start=int(start.timestamp()),
                stop=int(stop.timestamp()),
            )
        if agg_kind == "Y":
            start = datetime(year=now.year, month=1, day=1)
            stop = datetime(year=now.year + 1, month=1, day=1)
            return Timestamps(
                start=int(start.timestamp()),
                stop=int(stop.timestamp()),
            )
        raise AttributeError

    def restore_from_db(self, session: Session) -> None:
        """Restore data from database, typically after a process restart"""

        # retrieve last known period from DB
        lastPeriod = Persister.get_last_current_period(session)

        # if there is no last period, nothing to do
        if not lastPeriod:
            return

        # create/update KPIs values for all aggregation kinds which are updated once
        # per hour (D, W, M)
        if lastPeriod != self.current_period:
            for kpi in self.kpis:
                for agg_kind in ["D", "W", "M"]:
                    Processor.compute_kpi_values_for_aggregation_kind(
                        now=lastPeriod, kpi=kpi, agg_kind=agg_kind, session=session
                    )

        # create/update KPIs values for yearly aggregations
        # which are updated only once per day
        lastPeriod_day = lastPeriod.get_truncated_value("D")
        if self.current_day != lastPeriod_day:
            for kpi in self.kpis:
                Processor.compute_kpi_values_for_aggregation_kind(
                    now=lastPeriod, kpi=kpi, agg_kind="Y", session=session
                )
