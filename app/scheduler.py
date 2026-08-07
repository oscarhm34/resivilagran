"""Smart Shift Scheduler for La Vila Gran.

Generates monthly shift schedules using a 5-phase algorithm:
1. Apply rotation patterns (skip absences, preserve overrides)
2. Remove assignments that violate labor law constraints
3. Calculate coverage gaps vs requirements
4. Fill gaps using greedy algorithm with fairness scoring
5. Generate summary report
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from . import db
from .models import (
    Cleaner, ShiftType, ShiftAssignment, WorkerShiftConfig,
    Absence, ShiftCoverageRequirement,
)


class SmartScheduler:

    def __init__(self, year: int, month: int):
        import calendar
        self.year = year
        self.month = month
        self.num_days = calendar.monthrange(year, month)[1]
        self.first_day = date(year, month, 1)
        self.last_day = date(year, month, self.num_days)

        # Load data
        self.workers = Cleaner.query.filter_by(active=True).all()
        self.worker_ids = {w.id for w in self.workers}
        self.shift_types = {st.id: st for st in ShiftType.query.filter_by(active=True).all()}

        self.configs = {c.cleaner_id: c for c in WorkerShiftConfig.query.filter(
            WorkerShiftConfig.effective_until.is_(None),
        ).all()}

        # Build absence set: {(cleaner_id, date)}
        self.absent_days: set[tuple[int, date]] = set()
        absences = Absence.query.filter(
            Absence.start_date <= self.last_day,
            Absence.end_date >= self.first_day,
        ).all()
        for ab in absences:
            d = max(ab.start_date, self.first_day)
            while d <= min(ab.end_date, self.last_day):
                self.absent_days.add((ab.cleaner_id, d))
                d += timedelta(days=1)

        # Coverage requirements: {(shift_type_id, day_type): min_workers}
        self.coverage_reqs: dict[tuple[int, str], int] = {}
        for req in ShiftCoverageRequirement.query.all():
            self.coverage_reqs[(req.shift_type_id, req.day_type)] = req.min_workers

        # Working state: {(cleaner_id, date): shift_type_id or None}
        self.schedule: dict[tuple[int, date], int | None] = {}
        self.overrides: set[tuple[int, date]] = set()

        # Stats
        self.stats = {
            'generated_from_patterns': 0,
            'absences_skipped': 0,
            'overrides_preserved': 0,
            'invalid_removed': 0,
            'gaps_filled': 0,
            'gaps_remaining': 0,
            'total_assignments': 0,
        }

    def generate(self, preserve_overrides: bool = True, fill_gaps: bool = True,
                 created_by_id: int | None = None) -> dict:
        self._load_existing(preserve_overrides)
        self._phase1_apply_patterns()
        self._phase2_remove_invalid()
        if fill_gaps:
            gaps = self._phase3_calculate_gaps()
            self._phase4_fill_gaps(gaps)
        self._persist(created_by_id)
        return self._phase5_report()

    def _load_existing(self, preserve_overrides: bool):
        existing = ShiftAssignment.query.filter(
            ShiftAssignment.date >= self.first_day,
            ShiftAssignment.date <= self.last_day,
        ).all()
        for a in existing:
            if a.is_override and preserve_overrides:
                self.schedule[(a.cleaner_id, a.date)] = a.shift_type_id
                self.overrides.add((a.cleaner_id, a.date))
                self.stats['overrides_preserved'] += 1

    def _phase1_apply_patterns(self):
        for worker_id, config in self.configs.items():
            if worker_id not in self.worker_ids:
                continue
            pattern = config.pattern

            for d in range(1, self.num_days + 1):
                target_date = date(self.year, self.month, d)
                key = (worker_id, target_date)

                if key in self.overrides:
                    continue
                if key in self.absent_days:
                    self.stats['absences_skipped'] += 1
                    continue

                if config.fixed_shift_type_id:
                    self.schedule[key] = config.fixed_shift_type_id
                    self.stats['generated_from_patterns'] += 1
                elif pattern and pattern.days:
                    delta = (target_date - config.cycle_start_date).days
                    day_in_cycle = delta % pattern.cycle_days
                    if day_in_cycle < 0:
                        day_in_cycle += pattern.cycle_days
                    pattern_day = next(
                        (pd for pd in pattern.days if pd.day_number == day_in_cycle), None
                    )
                    st_id = pattern_day.shift_type_id if pattern_day else None
                    if st_id:
                        self.schedule[key] = st_id
                    self.stats['generated_from_patterns'] += 1

    def _phase2_remove_invalid(self):
        to_remove = []
        for (wid, d), st_id in list(self.schedule.items()):
            if not st_id or (wid, d) in self.overrides:
                continue
            if not self._check_rest(wid, d, st_id):
                to_remove.append((wid, d))
            elif self._week_hours(wid, d) > 40:
                to_remove.append((wid, d))
        for key in to_remove:
            del self.schedule[key]
            self.stats['invalid_removed'] += 1

    def _phase3_calculate_gaps(self) -> list[tuple[date, int, int]]:
        gaps = []
        for d in range(1, self.num_days + 1):
            target_date = date(self.year, self.month, d)
            is_weekend = target_date.weekday() >= 5
            for st_id in self.shift_types:
                current_count = sum(
                    1 for (wid, dd), sid in self.schedule.items()
                    if dd == target_date and sid == st_id
                )
                req = self._get_coverage_min(st_id, is_weekend)
                if req > current_count:
                    gap_size = req - current_count
                    gaps.append((target_date, st_id, gap_size))
        gaps.sort(key=lambda g: -g[2])
        return gaps

    def _phase4_fill_gaps(self, gaps: list[tuple[date, int, int]]):
        for target_date, st_id, gap_size in gaps:
            for _ in range(gap_size):
                best_worker = None
                best_score = -1
                for w in self.workers:
                    key = (w.id, target_date)
                    if key in self.schedule and self.schedule[key]:
                        continue
                    if key in self.absent_days:
                        continue
                    if not self._check_rest(w.id, target_date, st_id):
                        continue
                    if self._week_hours_with(w.id, target_date, st_id) > 40:
                        continue
                    if self._consecutive_days(w.id, target_date) >= 6:
                        continue

                    score = self._fairness_score(w.id, target_date, st_id)
                    if score > best_score:
                        best_score = score
                        best_worker = w.id

                if best_worker:
                    self.schedule[(best_worker, target_date)] = st_id
                    self.stats['gaps_filled'] += 1
                else:
                    self.stats['gaps_remaining'] += 1

    def _persist(self, created_by_id: int | None):
        # Delete non-override assignments for this month
        ShiftAssignment.query.filter(
            ShiftAssignment.date >= self.first_day,
            ShiftAssignment.date <= self.last_day,
            ShiftAssignment.is_override == False,
        ).delete()

        for (wid, d), st_id in self.schedule.items():
            if (wid, d) in self.overrides:
                continue
            if st_id:
                db.session.add(ShiftAssignment(
                    cleaner_id=wid, date=d, shift_type_id=st_id,
                    is_override=False, source='smart',
                    created_by=created_by_id,
                ))
                self.stats['total_assignments'] += 1

        # Count overrides in total
        self.stats['total_assignments'] += len(self.overrides)
        db.session.commit()

    def _phase5_report(self) -> dict:
        # Fairness stats
        worker_shifts = {}
        worker_weekends = {}
        worker_nights = {}
        night_ids = [sid for sid, st in self.shift_types.items()
                     if st.start_time.hour >= 20 or st.start_time.hour < 6]

        for (wid, d), st_id in self.schedule.items():
            if not st_id:
                continue
            worker_shifts[wid] = worker_shifts.get(wid, 0) + 1
            if d.weekday() >= 5:
                worker_weekends[wid] = worker_weekends.get(wid, 0) + 1
            if st_id in night_ids:
                worker_nights[wid] = worker_nights.get(wid, 0) + 1

        def _minmaxavg(d):
            vals = list(d.values()) if d else [0]
            return {'min': min(vals), 'max': max(vals), 'avg': round(sum(vals)/len(vals), 1)} if vals else {'min': 0, 'max': 0, 'avg': 0}

        # Coverage check
        coverage_status = []
        for d in range(1, self.num_days + 1):
            target_date = date(self.year, self.month, d)
            is_weekend = target_date.weekday() >= 5
            for st_id, st in self.shift_types.items():
                current = sum(1 for (wid, dd), sid in self.schedule.items()
                              if dd == target_date and sid == st_id)
                req = self._get_coverage_min(st_id, is_weekend)
                if req > 0 and current < req:
                    coverage_status.append({
                        'date': target_date.isoformat(),
                        'shift': st.short_name,
                        'have': current,
                        'need': req,
                    })

        return {
            'ok': True,
            **self.stats,
            'fairness': {
                'total_shifts': _minmaxavg(worker_shifts),
                'weekend_shifts': _minmaxavg(worker_weekends),
                'night_shifts': _minmaxavg(worker_nights),
            },
            'uncovered': coverage_status,
        }

    # ── Helper methods ──────────────────────────────────────────

    def _get_coverage_min(self, shift_type_id: int, is_weekend: bool) -> int:
        specific_key = (shift_type_id, 'weekend' if is_weekend else 'weekday')
        if specific_key in self.coverage_reqs:
            return self.coverage_reqs[specific_key]
        all_key = (shift_type_id, 'all')
        return self.coverage_reqs.get(all_key, 0)

    def _shift_hours(self, st_id: int) -> float:
        st = self.shift_types.get(st_id)
        if not st:
            return 0
        start = datetime.combine(date.today(), st.start_time)
        end = datetime.combine(date.today(), st.end_time)
        if end <= start:
            end += timedelta(days=1)
        return ((end - start).total_seconds() / 3600) - ((st.breaks_minutes or 0) / 60)

    def _check_rest(self, worker_id: int, target_date: date, st_id: int) -> bool:
        st = self.shift_types.get(st_id)
        if not st:
            return True
        # Check previous day
        prev_date = target_date - timedelta(days=1)
        prev_key = (worker_id, prev_date)
        prev_st_id = self.schedule.get(prev_key)
        if not prev_st_id:
            # Check DB for previous day (cross-month boundary)
            prev_assign = ShiftAssignment.query.filter_by(
                cleaner_id=worker_id, date=prev_date
            ).first()
            prev_st_id = prev_assign.shift_type_id if prev_assign else None

        if prev_st_id:
            prev_st = self.shift_types.get(prev_st_id)
            if prev_st:
                prev_end = datetime.combine(prev_date, prev_st.end_time)
                if prev_st.end_time <= prev_st.start_time:
                    prev_end += timedelta(days=1)
                curr_start = datetime.combine(target_date, st.start_time)
                rest = (curr_start - prev_end).total_seconds() / 3600
                if 0 <= rest < 12:
                    return False

        # Check next day
        next_date = target_date + timedelta(days=1)
        next_key = (worker_id, next_date)
        next_st_id = self.schedule.get(next_key)
        if next_st_id:
            next_st = self.shift_types.get(next_st_id)
            if next_st:
                curr_end = datetime.combine(target_date, st.end_time)
                if st.end_time <= st.start_time:
                    curr_end += timedelta(days=1)
                next_start = datetime.combine(next_date, next_st.start_time)
                rest = (next_start - curr_end).total_seconds() / 3600
                if 0 <= rest < 12:
                    return False
        return True

    def _week_hours(self, worker_id: int, target_date: date) -> float:
        monday = target_date - timedelta(days=target_date.weekday())
        total = 0.0
        for wd in range(7):
            d = monday + timedelta(days=wd)
            st_id = self.schedule.get((worker_id, d))
            if st_id:
                total += self._shift_hours(st_id)
        return total

    def _week_hours_with(self, worker_id: int, target_date: date, st_id: int) -> float:
        return self._week_hours(worker_id, target_date) + self._shift_hours(st_id)

    def _consecutive_days(self, worker_id: int, target_date: date) -> int:
        count = 0
        for offset in range(1, 8):
            d = target_date - timedelta(days=offset)
            key = (worker_id, d)
            if key in self.absent_days:
                break
            st_id = self.schedule.get(key)
            if st_id:
                count += 1
            else:
                break
        return count

    def _fairness_score(self, worker_id: int, target_date: date, st_id: int) -> float:
        score = 100.0
        # Fewer total shifts = higher score (balance workload)
        total = sum(1 for (wid, _), sid in self.schedule.items()
                    if wid == worker_id and sid)
        score -= total * 2

        # Weekend balance
        if target_date.weekday() >= 5:
            weekend_count = sum(1 for (wid, d), sid in self.schedule.items()
                                if wid == worker_id and sid and d.weekday() >= 5)
            score -= weekend_count * 5

        # Night balance
        night_ids = [sid for sid, st in self.shift_types.items()
                     if st.start_time.hour >= 20 or st.start_time.hour < 6]
        if st_id in night_ids:
            night_count = sum(1 for (wid, _), sid in self.schedule.items()
                              if wid == worker_id and sid in night_ids)
            score -= night_count * 5

        # Preference: worker's pattern includes this shift type
        config = self.configs.get(worker_id)
        if config and config.pattern:
            pattern_shifts = {pd.shift_type_id for pd in config.pattern.days if pd.shift_type_id}
            if st_id in pattern_shifts:
                score += 10

        return score
