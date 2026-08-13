"""Predictive ML models for resident risk assessment.

Hybrid approach:
- Rule-based weighted scoring (always available)
- scikit-learn models trained on historical data (when enough data exists)

Models:
1. Fall risk prediction
2. Pressure ulcer (UPP) risk prediction
3. Cognitive decline trajectory
"""
from __future__ import annotations
import os
import json
import pickle
from datetime import datetime, timedelta, date
from . import db
from .models import (
    Resident, AssessmentRecord, Incident, CareRecord,
    VitalSignReading, VitalSignType, MoodRecord, MedicationPrescription,
)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance', 'ml_models')


# ── Feature extraction ────────────────────────────────────────────────────

def _extract_features(resident_id: int) -> dict:
    """Extract all predictive features for a resident."""
    now = datetime.now()
    resident = db.session.get(Resident, resident_id)
    if not resident:
        return {}

    features = {}

    # -- Assessment scores --
    latest_norton = AssessmentRecord.query.filter_by(
        resident_id=resident_id, scale_type='norton',
    ).order_by(AssessmentRecord.assessed_at.desc()).first()
    features['norton_score'] = latest_norton.score if latest_norton else None

    latest_barthel = AssessmentRecord.query.filter_by(
        resident_id=resident_id, scale_type='barthel',
    ).order_by(AssessmentRecord.assessed_at.desc()).first()
    features['barthel_score'] = latest_barthel.score if latest_barthel else None

    # Barthel trend (last 3)
    barthel_history = AssessmentRecord.query.filter_by(
        resident_id=resident_id, scale_type='barthel',
    ).order_by(AssessmentRecord.assessed_at.desc()).limit(3).all()
    if len(barthel_history) >= 2:
        features['barthel_delta'] = barthel_history[0].score - barthel_history[-1].score
    else:
        features['barthel_delta'] = 0

    latest_pfeiffer = AssessmentRecord.query.filter_by(
        resident_id=resident_id, scale_type='pfeiffer',
    ).order_by(AssessmentRecord.assessed_at.desc()).first()
    features['pfeiffer_errors'] = latest_pfeiffer.score if latest_pfeiffer else None

    # Pfeiffer trend
    pfeiffer_history = AssessmentRecord.query.filter_by(
        resident_id=resident_id, scale_type='pfeiffer',
    ).order_by(AssessmentRecord.assessed_at.desc()).limit(3).all()
    if len(pfeiffer_history) >= 2:
        features['pfeiffer_delta'] = pfeiffer_history[0].score - pfeiffer_history[-1].score
    else:
        features['pfeiffer_delta'] = 0

    # -- Incident history --
    cutoff_90d = now - timedelta(days=90)
    cutoff_180d = now - timedelta(days=180)
    incidents_90d = Incident.query.filter(
        Incident.resident_id == resident_id,
        Incident.created_at >= cutoff_90d,
    ).all()
    features['falls_90d'] = sum(1 for i in incidents_90d if i.is_fall)
    features['incidents_90d'] = len(incidents_90d)

    incidents_180d = Incident.query.filter(
        Incident.resident_id == resident_id,
        Incident.created_at >= cutoff_180d,
    ).all()
    features['falls_180d'] = sum(1 for i in incidents_180d if i.is_fall)

    # -- Medication count --
    active_meds = MedicationPrescription.query.filter_by(
        resident_id=resident_id, active=True,
    ).count()
    features['medication_count'] = active_meds
    features['polypharmacy'] = 1 if active_meds >= 5 else 0

    # -- Dependency level --
    dep_map = {'autonomous': 0, 'mild': 1, 'moderate': 2, 'severe': 3, 'total': 4}
    features['dependency_numeric'] = dep_map.get(resident.dependency_level, 2)

    # -- Vital signs trends --
    # Weight trend
    weight_readings = db.session.query(VitalSignReading.value, CareRecord.start_time).join(
        CareRecord, VitalSignReading.care_record_id == CareRecord.id
    ).join(VitalSignType, VitalSignReading.vital_sign_type_id == VitalSignType.id).filter(
        CareRecord.resident_id == resident_id, VitalSignType.name.ilike('%peso%'),
    ).order_by(CareRecord.start_time.desc()).limit(5).all()

    if len(weight_readings) >= 2:
        latest_w = weight_readings[0].value
        oldest_w = weight_readings[-1].value
        features['weight_loss_pct'] = ((oldest_w - latest_w) / oldest_w * 100) if oldest_w > 0 else 0
    else:
        features['weight_loss_pct'] = 0

    # -- Care frequency --
    care_30d = CareRecord.query.filter(
        CareRecord.resident_id == resident_id,
        CareRecord.start_time >= now - timedelta(days=30),
    ).count()
    features['care_records_30d'] = care_30d

    # -- Mood trend --
    mood_records = MoodRecord.query.filter(
        MoodRecord.resident_id == resident_id,
        MoodRecord.recorded_at >= now - timedelta(days=14),
    ).order_by(MoodRecord.recorded_at.desc()).limit(10).all()
    if mood_records:
        features['avg_mood_14d'] = sum(m.mood_score for m in mood_records) / len(mood_records)
        features['mood_count_14d'] = len(mood_records)
    else:
        features['avg_mood_14d'] = 3.0  # neutral default
        features['mood_count_14d'] = 0

    return features


# ── Rule-based scoring (always available) ──────────────────────────────────

def _score_fall_risk(f: dict) -> tuple[float, str, list[str]]:
    """Score fall risk 0-100. Returns (score, level, factors)."""
    score = 0.0
    factors = []

    norton = f.get('norton_score')
    if norton is not None:
        if norton <= 9:
            score += 35
            factors.append(f'Norton molt baix ({norton}/20)')
        elif norton <= 12:
            score += 25
            factors.append(f'Norton baix ({norton}/20)')
        elif norton <= 14:
            score += 10

    barthel = f.get('barthel_score')
    if barthel is not None:
        if barthel <= 20:
            score += 20
            factors.append(f'Barthel total dependència ({barthel}/100)')
        elif barthel <= 40:
            score += 15
            factors.append(f'Barthel dependència severa ({barthel}/100)')
        elif barthel <= 60:
            score += 10

    falls_90 = f.get('falls_90d', 0)
    if falls_90 >= 3:
        score += 25
        factors.append(f'{falls_90} caigudes en 90 dies')
    elif falls_90 >= 1:
        score += 15
        factors.append(f'{falls_90} caiguda(es) en 90 dies')

    if f.get('polypharmacy'):
        score += 10
        factors.append(f'{f["medication_count"]} medicaments actius (polifarmàcia)')

    barthel_delta = f.get('barthel_delta', 0)
    if barthel_delta < -10:
        score += 10
        factors.append(f'Barthel empitjorant ({barthel_delta} punts)')

    level = 'high' if score >= 50 else ('medium' if score >= 25 else 'low')
    return min(score, 100), level, factors


def _score_upp_risk(f: dict) -> tuple[float, str, list[str]]:
    """Score pressure ulcer risk 0-100."""
    score = 0.0
    factors = []

    norton = f.get('norton_score')
    if norton is not None:
        if norton <= 9:
            score += 40
            factors.append(f'Norton risc molt alt ({norton}/20)')
        elif norton <= 12:
            score += 30
            factors.append(f'Norton risc alt ({norton}/20)')
        elif norton <= 14:
            score += 15
            factors.append(f'Norton risc mitjà ({norton}/20)')

    dep = f.get('dependency_numeric', 2)
    if dep >= 4:
        score += 20
        factors.append('Dependència total')
    elif dep >= 3:
        score += 15
        factors.append('Dependència severa')

    wl = f.get('weight_loss_pct', 0)
    if wl >= 10:
        score += 20
        factors.append(f'Pèrdua de pes significativa ({wl:.1f}%)')
    elif wl >= 5:
        score += 10
        factors.append(f'Pèrdua de pes moderada ({wl:.1f}%)')

    if f.get('care_records_30d', 0) < 10:
        score += 10
        factors.append('Poques atencions registrades (< 10 en 30 dies)')

    level = 'high' if score >= 50 else ('medium' if score >= 25 else 'low')
    return min(score, 100), level, factors


def _score_cognitive_risk(f: dict) -> tuple[float, str, list[str]]:
    """Score cognitive decline risk 0-100."""
    score = 0.0
    factors = []

    pfeiffer = f.get('pfeiffer_errors')
    if pfeiffer is not None:
        if pfeiffer >= 8:
            score += 40
            factors.append(f'Pfeiffer deteriorament sever ({pfeiffer} errors)')
        elif pfeiffer >= 5:
            score += 25
            factors.append(f'Pfeiffer deteriorament moderat ({pfeiffer} errors)')
        elif pfeiffer >= 3:
            score += 10
            factors.append(f'Pfeiffer deteriorament lleu ({pfeiffer} errors)')

    pfeiffer_delta = f.get('pfeiffer_delta', 0)
    if pfeiffer_delta >= 3:
        score += 25
        factors.append(f'Pfeiffer empitjorant ràpidament (+{pfeiffer_delta} errors)')
    elif pfeiffer_delta >= 1:
        score += 10
        factors.append(f'Pfeiffer empitjorant (+{pfeiffer_delta} errors)')

    mood = f.get('avg_mood_14d', 3)
    if mood < 2:
        score += 15
        factors.append(f'Ànim molt baix (mitjana {mood:.1f}/5)')
    elif mood < 2.5:
        score += 5

    dep = f.get('dependency_numeric', 2)
    if dep >= 3:
        score += 10

    level = 'high' if score >= 50 else ('medium' if score >= 25 else 'low')
    return min(score, 100), level, factors


def _score_nutritional_risk(f: dict) -> tuple[float, str, list[str]]:
    """Score nutritional risk 0-100."""
    score = 0.0
    factors = []

    wl = f.get('weight_loss_pct', 0)
    if wl >= 10:
        score += 45
        factors.append(f'Pèrdua de pes greu ({wl:.1f}%)')
    elif wl >= 5:
        score += 25
        factors.append(f'Pèrdua de pes moderada ({wl:.1f}%)')
    elif wl >= 3:
        score += 10

    dep = f.get('dependency_numeric', 2)
    if dep >= 4:
        score += 15
        factors.append('Dependència total (risc de desnutrició)')

    mood = f.get('avg_mood_14d', 3)
    if mood < 2:
        score += 15
        factors.append('Ànim molt baix (possible inapetència)')

    if f.get('polypharmacy'):
        score += 10
        factors.append('Polifarmàcia (pot afectar apetit)')

    level = 'high' if score >= 50 else ('medium' if score >= 25 else 'low')
    return min(score, 100), level, factors


# ── ML model training ──────────────────────────────────────────────────────

def _get_training_data() -> tuple[list, list, list]:
    """Build training dataset from historical data.
    Returns (feature_dicts, fall_labels, upp_labels).
    Labels: 1 if event occurred within 90 days after features snapshot.
    """
    residents = Resident.query.filter_by(active=True).all()
    X = []
    y_fall = []
    y_upp = []

    for r in residents:
        features = _extract_features(r.id)
        if not features or features.get('norton_score') is None:
            continue  # Need at least norton for meaningful prediction

        # Label: did they fall in the last 90 days?
        had_fall = 1 if features.get('falls_90d', 0) > 0 else 0
        # Label: Norton <= 12 is clinical UPP risk (proxy for actual UPP events)
        norton = features.get('norton_score', 20)
        had_upp_risk = 1 if norton <= 12 else 0

        X.append(features)
        y_fall.append(had_fall)
        y_upp.append(had_upp_risk)

    return X, y_fall, y_upp


def _features_to_array(features_list: list[dict]) -> list[list[float]]:
    """Convert feature dicts to numeric arrays for sklearn."""
    FEATURE_KEYS = [
        'norton_score', 'barthel_score', 'barthel_delta', 'pfeiffer_errors',
        'pfeiffer_delta', 'falls_90d', 'falls_180d', 'incidents_90d',
        'medication_count', 'polypharmacy', 'dependency_numeric',
        'weight_loss_pct', 'care_records_30d', 'avg_mood_14d',
    ]
    result = []
    for f in features_list:
        row = []
        for key in FEATURE_KEYS:
            val = f.get(key)
            row.append(float(val) if val is not None else 0.0)
        result.append(row)
    return result


def train_models() -> dict:
    """Train ML models on current data. Returns training report."""
    X_dicts, y_fall, y_upp = _get_training_data()

    if len(X_dicts) < 10:
        return {
            'status': 'insufficient_data',
            'message': f'Nomes {len(X_dicts)} residents amb dades suficients (minim 10)',
            'sample_size': len(X_dicts),
        }

    X = _features_to_array(X_dicts)

    os.makedirs(MODEL_DIR, exist_ok=True)
    report = {'sample_size': len(X), 'models': {}}

    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score

        # Fall risk model
        if sum(y_fall) >= 2:  # Need at least 2 positive cases
            model_fall = GradientBoostingClassifier(
                n_estimators=50, max_depth=3, random_state=42,
            )
            model_fall.fit(X, y_fall)
            scores = cross_val_score(model_fall, X, y_fall, cv=min(3, len(X)), scoring='accuracy')
            with open(os.path.join(MODEL_DIR, 'fall_risk.pkl'), 'wb') as f:
                pickle.dump(model_fall, f)
            report['models']['fall'] = {
                'accuracy': round(float(scores.mean()), 3),
                'positive_cases': int(sum(y_fall)),
            }

        # UPP risk model
        if sum(y_upp) >= 2:
            model_upp = GradientBoostingClassifier(
                n_estimators=50, max_depth=3, random_state=42,
            )
            model_upp.fit(X, y_upp)
            scores = cross_val_score(model_upp, X, y_upp, cv=min(3, len(X)), scoring='accuracy')
            with open(os.path.join(MODEL_DIR, 'upp_risk.pkl'), 'wb') as f:
                pickle.dump(model_upp, f)
            report['models']['upp'] = {
                'accuracy': round(float(scores.mean()), 3),
                'positive_cases': int(sum(y_upp)),
            }

        report['status'] = 'trained'
        report['trained_at'] = datetime.now().isoformat()

        # Save report
        with open(os.path.join(MODEL_DIR, 'training_report.json'), 'w') as f:
            json.dump(report, f, indent=2)

    except ImportError:
        report['status'] = 'sklearn_not_available'
        report['message'] = 'scikit-learn no instal·lat'

    return report


def _ml_predict(features: dict, model_name: str) -> float | None:
    """Get ML prediction probability if model exists."""
    model_path = os.path.join(MODEL_DIR, f'{model_name}.pkl')
    if not os.path.exists(model_path):
        return None
    try:
        with open(model_path, 'rb') as f:
            model = pickle.load(f)
        X = _features_to_array([features])
        proba = model.predict_proba(X)[0]
        # Return probability of positive class
        return float(proba[1]) if len(proba) > 1 else float(proba[0])
    except Exception:
        return None


# ── Public API ─────────────────────────────────────────────────────────────

def predict_risks(resident_id: int) -> dict:
    """Get comprehensive risk predictions combining rules + ML."""
    features = _extract_features(resident_id)
    if not features:
        return {}

    # Rule-based scores
    fall_score, fall_level, fall_factors = _score_fall_risk(features)
    upp_score, upp_level, upp_factors = _score_upp_risk(features)
    cog_score, cog_level, cog_factors = _score_cognitive_risk(features)
    nut_score, nut_level, nut_factors = _score_nutritional_risk(features)

    # ML predictions (if models exist)
    ml_fall = _ml_predict(features, 'fall_risk')
    ml_upp = _ml_predict(features, 'upp_risk')

    # Combine: if ML available, blend 60% rule + 40% ML
    if ml_fall is not None:
        combined_fall = fall_score * 0.6 + (ml_fall * 100) * 0.4
        fall_level = 'high' if combined_fall >= 50 else ('medium' if combined_fall >= 25 else 'low')
        fall_factors.append(f'Model ML: {ml_fall*100:.0f}% probabilitat')
    else:
        combined_fall = fall_score

    if ml_upp is not None:
        combined_upp = upp_score * 0.6 + (ml_upp * 100) * 0.4
        upp_level = 'high' if combined_upp >= 50 else ('medium' if combined_upp >= 25 else 'low')
        upp_factors.append(f'Model ML: {ml_upp*100:.0f}% probabilitat')
    else:
        combined_upp = upp_score

    return {
        'caidas': {
            'score': round(combined_fall, 1),
            'level': fall_level,
            'factors': fall_factors,
            'ml_available': ml_fall is not None,
        },
        'upp': {
            'score': round(combined_upp, 1),
            'level': upp_level,
            'factors': upp_factors,
            'ml_available': ml_upp is not None,
        },
        'cognitivo': {
            'score': round(cog_score, 1),
            'level': cog_level,
            'factors': cog_factors,
            'ml_available': False,
        },
        'nutricional': {
            'score': round(nut_score, 1),
            'level': nut_level,
            'factors': nut_factors,
            'ml_available': False,
        },
        'features': {k: v for k, v in features.items() if v is not None},
    }
