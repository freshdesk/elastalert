import datetime
import json

import requests

from elastalert.ruletypes import RuleType

# Pipeline/branch alerts use a lookback offset: root stage window ends at t1-lookback,
# other stages end at t1. Stage alerts use no offset.
_OP_SYMBOL = {
    'GREATER_THAN':    '>',
    'LESS_THAN':       '<',
    'GREATER_OR_EQUAL': '>=',
    'LESS_OR_EQUAL':   '<=',
}

_PIPELINE_BRANCH_TEMPLATES = frozenset([
    'pipeline_conversion_rate',
    'branch_conversion_rate',
    'pipeline_duration',
    'branch_duration',
])


def _fmt_ts(dt):
    """Format a UTC datetime as millisecond-precision ISO 8601."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03d' % (dt.microsecond // 1000) + 'Z'


def _stage_detail(stage):
    """Build a display dict for one query_payload stage entry.

    Returns:
        {
          'service':    str,
          'operation':  str,
          'filters':    [{'key': k, 'op': op, 'value': v}, ...]   # only non-empty
        }
    """
    keys   = stage.get('tag_keys',      []) or []
    values = stage.get('tag_values',    []) or []
    ops    = stage.get('tag_operators', []) or []
    filters = [
        {'key': k, 'op': o, 'value': v}
        for k, o, v in zip(keys, ops, values)
        if k  # skip blank entries
    ]
    return {
        'service':   stage.get('serviceName', ''),
        'operation': stage.get('operationName', ''),
        'filters':   filters,
    }


def _query_window(match):
    """Build the query_window dict for JSON description output."""
    if 'root_start_time' in match:
        return {
            'root': {'start': match['root_start_time'], 'end': match['root_end_time']},
            'full': {'start': match['query_start_time'], 'end': match['query_end_time']},
        }
    return {'start': match.get('query_start_time', '?'), 'end': match.get('query_end_time', '?')}


class FunnelAPIRuleType(RuleType):
    """Base for all funnel alert rule types.

    These rules call POST /api/v1/funnel/alert on haystack-router instead of
    querying Elasticsearch. The rule.yaml must include:

        funnel_api_url:   http://haystack-router:8080
        query_payload:    {stage_id1: {...}, stage_id2: {...}, ...}
        threshold:        <number>   (% for conversion/exception, ms for duration)
        timeFrame:        <int>      minutes — width of the query window
        index:            funnel     (required by loader schema but unused)

    Pipeline/branch rules additionally require:
        timeBuffer:       <int>  minutes — root stage window ends this far before t1;
                                 other stages still extend to t1.
    """

    required_options = frozenset([
        'funnel_api_url',
        'query_payload',
        'threshold',
        'timeFrame',
    ])

    # Subclasses declare the template name sent to the alert endpoint.
    alert_template = None

    def add_data(self, data):
        # Data comes from the funnel API, not from Elasticsearch hits.
        pass

    def run_api_check(self, rule, endtime):
        """Call the funnel alert endpoint and populate self.matches if threshold is breached.

        endtime is elastalert's computed query boundary (honours query_delay).
        Timestamps are computed here so the router receives explicit ISO bounds
        rather than computing from now() independently.
        """
        duration = datetime.timedelta(minutes=rule['timeFrame'])

        if self.alert_template in _PIPELINE_BRANCH_TEMPLATES:
            lookback = datetime.timedelta(minutes=rule['timeBuffer'])
            # Full query span: [t1 - timeBuffer - timeFrame, t1]
            # Root stage window: [t1 - timeBuffer - timeFrame, t1 - timeBuffer]
            start_time = endtime - lookback - duration
            end_time = endtime
            root_end_time = endtime - lookback
            time_buffer_minutes = rule['timeBuffer']
        else:
            start_time = endtime - duration
            end_time = endtime
            root_end_time = None
            time_buffer_minutes = None

        parameters = {
            'start_time': _fmt_ts(start_time),
            'end_time': _fmt_ts(end_time),
            **rule['query_payload'],
        }
        if time_buffer_minutes is not None:
            parameters['time_buffer_minutes'] = time_buffer_minutes
            parameters['root_start_time'] = _fmt_ts(start_time)
            parameters['root_end_time'] = _fmt_ts(root_end_time)

        payload = {
            'template': self.alert_template,
            'parameters': parameters,
        }

        url = rule['funnel_api_url'].rstrip('/') + '/api/v1/funnel/alert'
        timeout = rule.get('funnel_api_timeout', 30)

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise Exception('Funnel API request failed for rule %s: %s' % (rule['name'], e))

        matches_before = len(self.matches)
        self._check_threshold(rule, resp.json())

        # For pipeline/branch types the OSD link uses the root window; stage types use the full window.
        link_from = _fmt_ts(start_time)
        link_to = _fmt_ts(root_end_time) if root_end_time is not None else _fmt_ts(end_time)

        annotations = rule.get('alertmanager_annotations', {})
        osd_template = annotations.get('osd_link', '')
        if osd_template:
            pipeline_id = rule.get('alertmanager_labels', {}).get('pipeline_uuid', '')
            annotations['osd_link'] = (osd_template
                                       .replace('{pipeline_id}', pipeline_id)
                                       .replace('{from}', link_from)
                                       .replace('{to}', link_to))

        for match in self.matches[matches_before:]:
            match['query_start_time'] = _fmt_ts(start_time)
            match['query_end_time'] = _fmt_ts(end_time)
            if root_end_time is not None:
                match['root_start_time'] = _fmt_ts(start_time)
                match['root_end_time'] = _fmt_ts(root_end_time)

    def _operator_check(self, value, rule):
        """Return True if value breaches the threshold using threshold_operator.

        Supported values: GREATER_THAN, LESS_THAN, GREATER_OR_EQUAL, LESS_OR_EQUAL.
        Defaults to GREATER_THAN when threshold_operator is absent.
        """
        op = rule.get('threshold_operator', 'GREATER_THAN').upper()
        t = rule['threshold']
        if op == 'LESS_THAN':
            return value < t
        if op == 'LESS_OR_EQUAL':
            return value <= t
        if op == 'GREATER_OR_EQUAL':
            return value >= t
        return value > t  # GREATER_THAN (default)

    def _check_threshold(self, rule, data):
        raise NotImplementedError

    def _op_str(self, match):
        return _OP_SYMBOL.get(match.get('threshold_operator', 'GREATER_THAN'), '>')


class PipelineConversionRateRule(FunnelAPIRuleType):
    """Alert when the minimum conversion rate across all pipeline leaf stages drops below threshold (%).

    Uses the full pipeline stage graph in query_payload.
    Fires when min(leaf_spans / root_spans * 100) < threshold.
    """

    alert_template = 'pipeline_conversion_rate'
    required_options = FunnelAPIRuleType.required_options | frozenset(['timeBuffer'])

    def _check_threshold(self, rule, data):
        min_rate = data.get('min_conversion_rate', 100.0)
        if self._operator_check(min_rate, rule):
            payload = rule.get('query_payload', {})
            leaf_rates = []
            for leaf in data.get('leaf_conversion_rates', []):
                rate = leaf.get('conversion_rate', 0)
                if not self._operator_check(rate, rule):
                    continue
                sid = leaf.get('stage_id', '?')
                stage = payload.get('stage_id%s' % sid, {})
                detail = _stage_detail(stage)
                detail['conversion_rate'] = rate
                leaf_rates.append(detail)
            self.add_match({
                'alert_type':           'pipeline_conversion_rate',
                'pipeline_name':        rule.get('pipeline_name', rule['name']),
                'min_conversion_rate':  min_rate,
                'threshold':            rule['threshold'],
                'threshold_operator':   rule.get('threshold_operator', 'GREATER_THAN'),
                'leaf_conversion_rates': leaf_rates,
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':          'pipeline_conversion_rate',
            'pipeline':            match.get('pipeline_name', '?'),
            'min_conversion_rate': match.get('min_conversion_rate'),
            'threshold':           '%s %s' % (self._op_str(match), match.get('threshold')),
            'leaf_stages':         match.get('leaf_conversion_rates', []),
            'query_window':        _query_window(match),
        }, indent=2)


class BranchConversionRateRule(FunnelAPIRuleType):
    """Alert when conversion rate from root to the end of a branch drops below threshold (%).

    query_payload should define a linear path (one leaf stage).
    Fires when leaf_spans / root_spans * 100 < threshold.
    """

    alert_template = 'branch_conversion_rate'
    required_options = FunnelAPIRuleType.required_options | frozenset(['timeBuffer'])

    def _check_threshold(self, rule, data):
        rate = data.get('conversion_rate', 100.0)
        if self._operator_check(rate, rule):
            # For branch rules the stage list is linear; collect all stages as the branch path.
            payload = rule.get('query_payload', {})
            stages = [_stage_detail(s) for s in payload.values()]
            self.add_match({
                'alert_type':         'branch_conversion_rate',
                'pipeline_name':      rule.get('pipeline_name', rule['name']),
                'conversion_rate':    rate,
                'threshold':          rule['threshold'],
                'threshold_operator': rule.get('threshold_operator', 'GREATER_THAN'),
                'stages':             stages,
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':      'branch_conversion_rate',
            'pipeline':        match.get('pipeline_name', '?'),
            'conversion_rate': match.get('conversion_rate'),
            'threshold':       '%s %s' % (self._op_str(match), match.get('threshold')),
            'stages':          match.get('stages', []),
            'query_window':    _query_window(match),
        }, indent=2)


class PipelineDurationRule(FunnelAPIRuleType):
    """Alert when the p95 end-to-end latency across all pipeline traces exceeds threshold (ms).

    p95 is computed over all traces regardless of which branch they took.
    Uses the full pipeline stage graph in query_payload.
    """

    alert_template = 'pipeline_duration'
    required_options = FunnelAPIRuleType.required_options | frozenset(['timeBuffer'])

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_e2e_latency_ms', 0.0)
        if self._operator_check(p95, rule):
            self.add_match({
                'alert_type':        'pipeline_duration',
                'pipeline_name':     rule.get('pipeline_name', rule['name']),
                'p95_e2e_latency_ms': p95,
                'threshold':         rule['threshold'],
                'threshold_operator': rule.get('threshold_operator', 'GREATER_THAN'),
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':        'pipeline_duration',
            'pipeline':          match.get('pipeline_name', '?'),
            'p95_e2e_latency_ms': match.get('p95_e2e_latency_ms'),
            'threshold':         '%s %s' % (self._op_str(match), match.get('threshold')),
            'query_window':      _query_window(match),
        }, indent=2)


class BranchDurationRule(FunnelAPIRuleType):
    """Alert when the p95 end-to-end latency from root to the branch leaf exceeds threshold (ms).

    query_payload should define a linear path (one leaf stage).
    """

    alert_template = 'branch_duration'
    required_options = FunnelAPIRuleType.required_options | frozenset(['timeBuffer'])

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_e2e_latency_ms', 0.0)
        if self._operator_check(p95, rule):
            self.add_match({
                'alert_type':        'branch_duration',
                'pipeline_name':     rule.get('pipeline_name', rule['name']),
                'p95_e2e_latency_ms': p95,
                'threshold':         rule['threshold'],
                'threshold_operator': rule.get('threshold_operator', 'GREATER_THAN'),
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':        'branch_duration',
            'pipeline':          match.get('pipeline_name', '?'),
            'p95_e2e_latency_ms': match.get('p95_e2e_latency_ms'),
            'threshold':         '%s %s' % (self._op_str(match), match.get('threshold')),
            'query_window':      _query_window(match),
        }, indent=2)


class StageDurationRule(FunnelAPIRuleType):
    """Alert when the p95 span duration of a single stage exceeds threshold (ms).

    query_payload should define exactly one stage.
    Duration is the span's own duration — not end-to-end from root.
    No timeBuffer offset: window is [t1 - timeFrame, t1].
    """

    alert_template = 'stage_duration'

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_latency_ms', 0.0)
        if self._operator_check(p95, rule):
            sid = data.get('stage_id')
            payload = rule.get('query_payload', {})
            stage = payload.get('stage_id%s' % sid) or next(iter(payload.values()), {})
            self.add_match({
                'alert_type':        'stage_duration',
                'pipeline_name':     rule.get('pipeline_name', rule['name']),
                'stage':             _stage_detail(stage),
                'p95_latency_ms':    p95,
                'threshold':         rule['threshold'],
                'threshold_operator': rule.get('threshold_operator', 'GREATER_THAN'),
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':     'stage_duration',
            'pipeline':       match.get('pipeline_name', '?'),
            'p95_latency_ms': match.get('p95_latency_ms'),
            'threshold':      '%s %s' % (self._op_str(match), match.get('threshold')),
            'stage':          match.get('stage', {}),
            'query_window':   _query_window(match),
        }, indent=2)


class StageExceptionRateRule(FunnelAPIRuleType):
    """Alert when the exception rate (error_count / span_count * 100) of a stage exceeds threshold (%).

    query_payload should define exactly one stage.
    No timeBuffer offset: window is [t1 - timeFrame, t1].
    """

    alert_template = 'exception_rate'

    def _check_threshold(self, rule, data):
        rate = data.get('exception_rate', 0.0)
        if self._operator_check(rate, rule):
            sid = data.get('stage_id')
            payload = rule.get('query_payload', {})
            stage = payload.get('stage_id%s' % sid) or next(iter(payload.values()), {})
            self.add_match({
                'alert_type':        'exception_rate',
                'pipeline_name':     rule.get('pipeline_name', rule['name']),
                'stage':             _stage_detail(stage),
                'exception_rate':    rate,
                'error_count':       data.get('error_count'),
                'span_count':        data.get('span_count'),
                'threshold':         rule['threshold'],
                'threshold_operator': rule.get('threshold_operator', 'GREATER_THAN'),
            })

    def get_match_str(self, match):
        return json.dumps({
            'alert_type':    'exception_rate',
            'pipeline':      match.get('pipeline_name', '?'),
            'exception_rate': match.get('exception_rate'),
            'error_count':   match.get('error_count'),
            'span_count':    match.get('span_count'),
            'threshold':     '%s %s' % (self._op_str(match), match.get('threshold')),
            'stage':         match.get('stage', {}),
            'query_window':  _query_window(match),
        }, indent=2)
