"""
Marshmallow schemas for API request validation and response documentation.
Generated from models.py dataclasses where possible.
"""

from __future__ import annotations

from marshmallow import Schema, fields
from marshmallow_dataclass import class_schema

from .. import models

# --- Base Model Schemas (Auto-generated from models.py) ---

RoomSchema = class_schema(models.Room)
RoomSensorSchema = class_schema(models.RoomSensor)
RoomVentSchema = class_schema(models.RoomVent)
RoomPresenceSensorSchema = class_schema(models.RoomPresenceSensor)
ScheduleSchema = class_schema(models.Schedule)
ThermostatConfigSchema = class_schema(models.ThermostatConfig)
RoomOverrideSchema = class_schema(models.RoomOverride)
ZoneStatusSchema = class_schema(models.ZoneStatus)
RoomLiveStateSchema = class_schema(models.RoomLiveState)
CycleTempSampleSchema = class_schema(models.CycleTempSample)


# --- Custom Request/Response Schemas ---


class ErrorSchema(Schema):
    error = fields.Str(required=True)


class SuccessSchema(Schema):
    ok = fields.Bool(dump_default=True)


class RestoredSchema(Schema):
    restored = fields.Bool(dump_default=True)


class ClearedSchema(Schema):
    cleared = fields.Bool(dump_default=True)


class DeletedSchema(Schema):
    deleted = fields.Str()


class DeletedTrueSchema(Schema):
    deleted = fields.Bool(dump_default=True)


class UpdatedSchema(Schema):
    updated = fields.Bool(dump_default=True)


class RoomResponseSchema(RoomSchema):
    sensors = fields.List(fields.Nested(RoomSensorSchema))
    vents = fields.List(fields.Nested(RoomVentSchema))
    presence_sensors = fields.List(fields.Nested(RoomPresenceSensorSchema))
    schedules = fields.List(fields.Nested(ScheduleSchema))


class RoomVentUpdateSchema(Schema):
    control_method = fields.Str(required=True)


class RoomVentUpdateResponseSchema(UpdatedSchema):
    control_method = fields.Str()


class VentTestSchema(Schema):
    entity_id = fields.Str(required=True)
    control_method = fields.Str(required=True)
    direction = fields.Str(required=True)


class RoomOverrideRequestSchema(Schema):
    target_temp = fields.Float(required=True)
    duration_hours = fields.Float(load_default=2.0)


class RoomActiveStatusSchema(Schema):
    room_id = fields.Str()
    source = fields.Str()  # 'schedule' | 'presence' | 'override' | 'idle'
    target_temp = fields.Float(allow_none=True)
    ends_in_seconds = fields.Int(allow_none=True)
    next_schedule_in_seconds = fields.Int(allow_none=True)
    next_schedule_target = fields.Float(allow_none=True)
    next_schedule_label = fields.Str(allow_none=True)


class RoomActiveStatusRequestSchema(Schema):
    room_ids = fields.List(fields.Str(), required=True)


class RoomActiveStatusResponseSchema(Schema):
    # Keyed by room_id; value is RoomActiveStatus
    room_statuses = fields.Dict(keys=fields.Str(), values=fields.Nested(RoomActiveStatusSchema))


class EntityStateSchema(Schema):
    state = fields.Str()
    numeric = fields.Float(allow_none=True)
    unit = fields.Str()
    attributes = fields.Dict()


class EntityStateResponseSchema(Schema):
    # Keyed by entity_id; value is EntityState
    entity_states = fields.Dict(keys=fields.Str(), values=fields.Nested(EntityStateSchema))


class HAEntitySchema(Schema):
    entity_id = fields.Str()
    state = fields.Str(allow_none=True)
    friendly_name = fields.Str()


class CycleLogResponseSchema(Schema):
    id = fields.Str()
    thermostat_entity_id = fields.Str()
    started_at = fields.DateTime()
    ended_at = fields.DateTime(allow_none=True)
    mode = fields.Str()
    rooms = fields.Dict()  # JSON object in response
    ended_reason = fields.Str(allow_none=True)
    thermostat_temp_at_start = fields.Float(allow_none=True)
    thermostat_temp_at_end = fields.Float(allow_none=True)
    setpoint_at_start = fields.Float(allow_none=True)
    setpoint_at_end = fields.Float(allow_none=True)
    vents_at_start = fields.Dict(allow_none=True)
    vents_at_end = fields.Dict(allow_none=True)


class CycleRoomDetailSchema(Schema):
    room_id = fields.Str()
    name = fields.Str(allow_none=True)
    source = fields.Str(allow_none=True)
    target_temp = fields.Float()
    reached_at = fields.DateTime(allow_none=True)
    vent_closed_at = fields.DateTime(allow_none=True)
    temp_at_start = fields.Float(allow_none=True)
    temp_at_end = fields.Float(allow_none=True)
    trigger_detail = fields.Dict(allow_none=True)
    joined_at = fields.DateTime(allow_none=True)


class CycleVentEventSchema(Schema):
    id = fields.Int()
    timestamp = fields.DateTime()
    entity_id = fields.Str()
    room_id = fields.Str(allow_none=True)
    action = fields.Str()
    reason = fields.Str(allow_none=True)


class CycleSetpointHistorySchema(Schema):
    id = fields.Int()
    timestamp = fields.DateTime()
    setpoint = fields.Float()
    reason = fields.Str(allow_none=True)


class CycleDetailResponseSchema(Schema):
    cycle = fields.Nested(CycleLogResponseSchema)
    rooms = fields.List(fields.Nested(CycleRoomDetailSchema))
    vent_events = fields.List(fields.Nested(CycleVentEventSchema))
    setpoint_history = fields.List(fields.Nested(CycleSetpointHistorySchema))


class EventLogEntrySchema(Schema):
    id = fields.Int()
    timestamp = fields.DateTime()
    level = fields.Str()
    category = fields.Str()
    message = fields.Str()
    details = fields.Dict(allow_none=True)


class SystemStatusSchema(Schema):
    enabled = fields.Bool()
    dev_mode = fields.Bool()


class LogRetentionSettingsSchema(Schema):
    event_log_retention_days = fields.Int()
    cycle_log_retention_days = fields.Int()


class OutsideTempEntitySettingSchema(Schema):
    entity_id = fields.Str(allow_none=True)
    current_value = fields.Float(allow_none=True)


class AppSettingsSchema(Schema):
    temperature_unit = fields.Str()
    unit_change_ack_required = fields.Bool()


class UnitChangeAckResponseSchema(Schema):
    unit_change_ack_required = fields.Bool(dump_default=False)


class RestartResponseSchema(Schema):
    restarting = fields.Bool(dump_default=True)


# --- Metrics Schemas ---


class MetricsSummarySchema(Schema):
    start_date = fields.Str()
    end_date = fields.Str()
    thermostat_entity_id = fields.Str(allow_none=True)
    heating_seconds = fields.Int()
    cooling_seconds = fields.Int()
    cycle_count = fields.Int()
    completed_count = fields.Int()
    timeout_count = fields.Int()
    aborted_count = fields.Int()
    avg_cycle_duration_seconds = fields.Float(allow_none=True)
    duty_cycle_pct = fields.Float()
    avg_outside_temp_at_start = fields.Float(allow_none=True)
    avg_outside_temp_at_end = fields.Float(allow_none=True)
    thermostat_count = fields.Int()
    source_breakdown = fields.Dict()


class MetricsTimeseriesPointSchema(Schema):
    period = fields.Str()
    value = fields.Float(allow_none=True)
    heating_seconds = fields.Int(required=False)
    cooling_seconds = fields.Int(required=False)


class MetricsTimeseriesSchema(Schema):
    thermostat_entity_id = fields.Str()
    metric = fields.Str()
    granularity = fields.Str()
    start = fields.Str()
    end = fields.Str()
    series = fields.List(fields.Nested(MetricsTimeseriesPointSchema))


class RoomMetricSchema(Schema):
    room_id = fields.Str()
    room_name = fields.Str()
    participation_count = fields.Int()
    participation_rate = fields.Float()
    heating_seconds = fields.Int()
    cooling_seconds = fields.Int()
    avg_time_to_target_seconds = fields.Float(allow_none=True)


class RoomMetricsResponseSchema(Schema):
    thermostat_entity_id = fields.Str()
    start = fields.Str()
    end = fields.Str()
    rooms = fields.List(fields.Nested(RoomMetricSchema))


class CyclesVsOutsideTempPointSchema(Schema):
    cycle_id = fields.Str()
    mode = fields.Str()
    outside_temp = fields.Float()
    duration_minutes = fields.Float()
    started_at = fields.Str()


class CyclesVsOutsideTempResponseSchema(Schema):
    thermostat_entity_id = fields.Str()
    start = fields.Str()
    end = fields.Str()
    points = fields.List(fields.Nested(CyclesVsOutsideTempPointSchema))


class OvershootHistogramSchema(Schema):
    thermostat_entity_id = fields.Str()
    start_date = fields.Str()
    end_date = fields.Str()
    bin_size = fields.Float()
    labels = fields.List(fields.Str())
    counts = fields.List(fields.Int())
    total_room_cycles = fields.Int()
    overshot_count = fields.Int()
    overshot_pct = fields.Float()
    max_overshoot_f = fields.Float()
    avg_overshoot_f = fields.Float()


class HourHeatmapSchema(Schema):
    start_date = fields.Str()
    end_date = fields.Str()
    thermostat_entity_id = fields.Str()
    day_labels = fields.List(fields.Str())
    grid_seconds = fields.List(fields.List(fields.Int()))


class VentTimelineEventSchema(Schema):
    cycle_id = fields.Str()
    timestamp = fields.Str()
    entity_id = fields.Str()
    room_id = fields.Str(allow_none=True)
    action = fields.Str()
    reason = fields.Str(allow_none=True)
    cycle_mode = fields.Str()
    cycle_started_at = fields.Str()
    cycle_ended_at = fields.Str()


class VentTimelineResponseSchema(Schema):
    thermostat_entity_id = fields.Str()
    start = fields.Str()
    end = fields.Str()
    note = fields.Str()
    events = fields.List(fields.Nested(VentTimelineEventSchema))


class MetricsLiveSchema(Schema):
    thermostat_entity_id = fields.Str()
    as_of = fields.Str()
    today = fields.Nested(MetricsSummarySchema)
    current_cycle = fields.Dict(allow_none=True)
    outside_temp_entity_id = fields.Str(allow_none=True)
    current_outside_temp = fields.Float(allow_none=True)
