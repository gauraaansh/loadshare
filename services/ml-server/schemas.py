"""
ARIA — ML Server: Pydantic Schemas
====================================
Request and response models for all 4 internal prediction endpoints.

Design principles:
  - Every field has a description (self-documenting API)
  - Response always includes key_factors (top SHAP features) so
    the calling agent can generate explanations without a second call
  - model_version echoed back so the MCP server can log which
    artifact version produced the prediction
"""

from pydantic import BaseModel, Field
from typing import Optional


# ══════════════════════════════════════════════════════════════
# SHARED
# ══════════════════════════════════════════════════════════════

class KeyFactor(BaseModel):
    feature:    str   = Field(..., description="Feature name")
    importance: float = Field(..., description="Mean |SHAP| value — higher = more influential")


# ══════════════════════════════════════════════════════════════
# MODEL 1 — Rider Persona Classifier
# POST /internal/predict/persona
# ══════════════════════════════════════════════════════════════

class PersonaRequest(BaseModel):
    # Core behavioural signals — collected from first 5-10 rides
    n_rides_observed:      int   = Field(..., ge=1,  description="Number of rides this persona is based on")
    peak_hour_rate:        float = Field(..., ge=0.0, le=1.0, description="Fraction of rides during peak hours (7-10, 18-22)")
    morning_rate:          float = Field(..., ge=0.0, le=1.0, description="Fraction of rides in morning window (6-11)")
    night_rate:            float = Field(..., ge=0.0, le=1.0, description="Fraction of rides after 22:00")
    n_distinct_zones:      int   = Field(..., ge=1,  description="Number of distinct zones rider has worked in")
    acceptance_rate:       float = Field(..., ge=0.0, le=1.0, description="Order acceptance rate 0-1")
    ld_rejection_rate:     float = Field(..., ge=0.0, le=1.0, description="Long-distance order rejection rate 0-1")
    avg_shift_hours:       float = Field(..., ge=0.0,          description="Average active hours per session")
    off_peak_acceptance:   float = Field(..., ge=0.0, le=1.0, description="Acceptance rate during off-peak hours")
    avg_orders_per_shift:  float = Field(..., ge=0.0,          description="Average completed orders per shift")

    class Config:
        json_schema_extra = {"example": {
            "n_rides_observed": 8,
            "peak_hour_rate": 0.65,
            "morning_rate": 0.20,
            "night_rate": 0.05,
            "n_distinct_zones": 2,
            "acceptance_rate": 0.78,
            "ld_rejection_rate": 0.72,
            "avg_shift_hours": 4.5,
            "off_peak_acceptance": 0.15,
            "avg_orders_per_shift": 9.0,
        }}


class PersonaResponse(BaseModel):
    persona:            str   = Field(..., description="'supplementary' or 'dedicated'")
    persona_label:      int   = Field(..., description="0=supplementary, 1=dedicated")
    confidence:         float = Field(..., description="Model confidence 0-1 for the predicted class")
    dedicated_prob:     float = Field(..., description="Raw probability of dedicated persona")
    supplementary_prob: float = Field(..., description="Raw probability of supplementary persona")
    key_factors:        list[KeyFactor] = Field(..., description="Top 3 SHAP features driving this prediction")
    model_version:      str   = Field(..., description="Model artifact version string")


# ══════════════════════════════════════════════════════════════
# MODEL 2 — Delivery Duration Scorer
# POST /internal/predict/duration
# ══════════════════════════════════════════════════════════════

class DurationRequest(BaseModel):
    # Order features
    distance_km:             float = Field(..., ge=0.0,  description="Haversine distance restaurant→customer (km)")
    Road_traffic_density_enc: int  = Field(..., ge=0, le=3, description="0=Low, 1=Medium, 2=High, 3=Jam")
    order_hour:              int   = Field(..., ge=0, le=23, description="Hour order was placed (0-23)")
    is_lunch_peak:           int   = Field(..., ge=0, le=1,  description="1 if order placed 12-14h")
    is_dinner_peak:          int   = Field(..., ge=0, le=1,  description="1 if order placed 18-21h")
    is_weekend:              int   = Field(..., ge=0, le=1,  description="1 if Saturday or Sunday")
    day_of_week:             int   = Field(..., ge=0, le=6,  description="0=Monday, 6=Sunday")
    month:                   int   = Field(..., ge=1, le=12, description="Month (1-12)")

    # Geography
    City_enc:                int   = Field(..., ge=0, le=2,  description="0=Urban, 1=Metropolitan, 2=Semi-Urban")
    city_name_enc:           int   = Field(..., ge=0, le=21, description="City identifier 0-21")

    # Order characteristics
    Weatherconditions_enc:   int   = Field(..., ge=0, le=5,  description="Weather condition encoding 0-5")
    Type_of_vehicle_enc:     int   = Field(..., ge=0, le=3,  description="0=bicycle, 1=e-scooter, 2=scooter, 3=motorcycle")
    Type_of_order_enc:       int   = Field(..., ge=0, le=3,  description="0=Snack, 1=Drinks, 2=Meal, 3=Buffet")
    Festival_enc:            int   = Field(..., ge=0, le=1,  description="0=No festival, 1=Festival day")
    multiple_deliveries:     int   = Field(..., ge=0, le=3,  description="Number of simultaneous deliveries 0-3")

    # Rider attributes
    Delivery_person_Age:     int   = Field(..., ge=18, le=65, description="Rider age")
    Delivery_person_Ratings: float = Field(..., ge=1.0, le=5.0, description="Rider platform rating 1.0-5.0")
    Vehicle_condition:       int   = Field(..., ge=0, le=3,  description="Vehicle condition score 0-3")

    class Config:
        json_schema_extra = {"example": {
            "distance_km": 4.2,
            "Road_traffic_density_enc": 2,
            "order_hour": 19,
            "is_lunch_peak": 0,
            "is_dinner_peak": 1,
            "is_weekend": 1,
            "day_of_week": 5,
            "month": 7,
            "City_enc": 1,
            "city_name_enc": 3,
            "Weatherconditions_enc": 0,
            "Type_of_vehicle_enc": 3,
            "Type_of_order_enc": 2,
            "Festival_enc": 0,
            "multiple_deliveries": 0,
            "Delivery_person_Age": 28,
            "Delivery_person_Ratings": 4.6,
            "Vehicle_condition": 2,
        }}


class DurationResponse(BaseModel):
    predicted_minutes:  float = Field(..., description="Predicted delivery duration in minutes")
    baseline_minutes:   Optional[float] = Field(None, description="Historical baseline for this route/time if available")
    deviation_minutes:  Optional[float] = Field(None, description="predicted - baseline; positive = slower than expected")
    key_factors:        list[KeyFactor] = Field(..., description="Top 5 SHAP features driving this prediction")
    model_version:      str   = Field(..., description="Model artifact version string")


# ══════════════════════════════════════════════════════════════
# MODEL 3 — Dead Zone Risk Predictor
# POST /internal/predict/dead-zone
# ══════════════════════════════════════════════════════════════

class DeadZoneRequest(BaseModel):
    dest_zone_type_enc:       int   = Field(..., ge=0, le=3,  description="Zone type encoding: 0=hub, 1=commercial, 2=residential, 3=peripheral (canonical: algorithms/constants.py ZONE_TYPE_ENC)")
    city_tier_enc:            int   = Field(..., ge=0, le=2,  description="0=Metropolitan, 1=Urban, 2=Semi-Urban")
    hour_of_day:              int   = Field(..., ge=0, le=23, description="Current hour (0-23)")
    day_of_week:              int   = Field(..., ge=0, le=6,  description="0=Monday, 6=Sunday")
    is_weekend:               int   = Field(..., ge=0, le=1,  description="1 if Saturday or Sunday")
    is_ld_order:              int   = Field(..., ge=0, le=1,  description="1 if long-distance order (crosses zone boundary)")
    dist_from_home_zone_km:   float = Field(..., ge=0.0,      description="Distance destination is from rider's home zone (km)")
    current_density_ratio:    float = Field(..., ge=0.0,      description="Current orders/capacity ratio at destination zone")
    historical_dead_rate:     float = Field(..., ge=0.0, le=1.0, description="Historical dead zone rate for this zone/time combo")

    class Config:
        json_schema_extra = {"example": {
            "dest_zone_type_enc": 3,
            "city_tier_enc": 0,
            "hour_of_day": 14,
            "day_of_week": 2,
            "is_weekend": 0,
            "is_ld_order": 1,
            "dist_from_home_zone_km": 6.5,
            "current_density_ratio": 0.25,
            "historical_dead_rate": 0.61,
        }}


class DeadZoneResponse(BaseModel):
    dead_zone_probability:      float = Field(..., description="Calibrated probability 0-1 of stranding at destination")
    is_high_risk:               bool  = Field(..., description="True if probability exceeds 0.55 threshold")
    expected_stranding_mins:    Optional[float] = Field(None, description="Predicted stranding time if dead zone occurs (minutes). None if low risk.")
    expected_eph_loss:          Optional[float] = Field(None, description="Estimated EPH loss from stranding. None if low risk.")
    key_factors:                list[KeyFactor] = Field(..., description="Top 3 SHAP features from classifier")
    model_version:              str   = Field(..., description="Model artifact version string")


# ══════════════════════════════════════════════════════════════
# MODEL 4 — Earnings Trajectory Forecaster
# POST /internal/predict/earnings-trajectory
# ══════════════════════════════════════════════════════════════

class EarningsRequest(BaseModel):
    # Rider context
    persona_enc:          int   = Field(..., ge=0, le=1,  description="0=supplementary, 1=dedicated")
    hour_of_day:          int   = Field(..., ge=0, le=23, description="Current hour")

    # Session progress
    orders_completed:     int   = Field(..., ge=0,        description="Orders completed so far this session")
    earnings_so_far:      float = Field(..., ge=0.0,      description="Earnings accumulated so far (Rs.)")
    current_eph:          float = Field(..., ge=0.0,      description="Current earnings per hour (Rs./hr)")
    idle_time_mins:       float = Field(..., ge=0.0,      description="Total idle minutes so far this session")
    dead_runs_count:      int   = Field(..., ge=0,        description="Number of dead run events this session")
    zone_density:         float = Field(..., ge=0.0,      description="Current zone order density ratio")

    # Time window
    obs_point_mins:       float = Field(..., ge=0.0,      description="Minutes elapsed since session start")
    time_remaining_mins:  float = Field(..., ge=0.0,      description="Minutes remaining in planned shift")
    total_shift_mins:     float = Field(..., ge=0.0,      description="Total planned shift length (minutes)")

    # Lag EPH values — trajectory context
    eph_lag1_30min:       float = Field(..., ge=0.0,      description="EPH 30 minutes ago (0 if session < 30 min)")
    eph_lag2_60min:       float = Field(..., ge=0.0,      description="EPH 60 minutes ago (0 if session < 60 min)")
    eph_lag3_90min:       float = Field(..., ge=0.0,      description="EPH 90 minutes ago (0 if session < 90 min)")

    class Config:
        json_schema_extra = {"example": {
            "persona_enc": 0,
            "hour_of_day": 20,
            "orders_completed": 5,
            "earnings_so_far": 210.0,
            "current_eph": 76.5,
            "idle_time_mins": 28.0,
            "dead_runs_count": 1,
            "zone_density": 0.42,
            "obs_point_mins": 165.0,
            "time_remaining_mins": 75.0,
            "total_shift_mins": 240.0,
            "eph_lag1_30min": 82.0,
            "eph_lag2_60min": 88.0,
            "eph_lag3_90min": 91.0,
        }}


class EarningsResponse(BaseModel):
    projected_final_eph:    float = Field(..., description="Projected end-of-shift EPH (Rs./hr)")
    current_eph:            float = Field(..., description="Current EPH echoed back for convenience")
    below_threshold:        bool  = Field(..., description="True if projected EPH will fall below Rs.90 target")
    eph_trend:              str   = Field(..., description="'improving', 'stable', or 'declining' based on lag features")
    alert_level:            str   = Field(..., description="'none', 'watch', or 'intervene'")
    eph_slope:              float = Field(..., description="EPH change over last 30 minutes (current - lag1)")
    key_factors:            list[KeyFactor] = Field(..., description="Top 3 SHAP features from regressor")
    model_version:          str   = Field(..., description="Model artifact version string")


# ══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════

class ModelStatus(BaseModel):
    loaded:       bool
    version:      Optional[str] = None
    artifact_dir: Optional[str] = None


class HealthResponse(BaseModel):
    status:  str = Field(..., description="'healthy' or 'degraded'")
    models:  dict[str, ModelStatus]
    uptime_seconds: float
