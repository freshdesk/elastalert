import datetime

import requests

from elastalert.ruletypes import RuleType

# Pipeline/branch alerts use a lookback offset: root stage window ends at t1-lookback,
# other stages end at t1. Stage alerts use no offset.
_PIPELINE_BRANCH_TEMPLATES = frozenset([
    'pipeline_conversion_rate',
    'branch_conversion_rate',
    'pipeline_duration',
    'branch_duration',
])


def _fmt_ts(dt):
    """Format a UTC datetime as millisecond-precision ISO 8601."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03d' % (dt.microsecond // 1000) + 'Z'


class FunnelAPIRuleType(RuleType):
    """Base for all funnel alert rule types.

    These rules call POST /api/v1/funnel/alert on haystack-router instead of
    querying Elasticsearch. The rule.yaml must include:

        funnel_api_url:   http://haystack-router:8080
        query_payload:    {stage_id1: {...}, stage_id2: {...}, ...}
        threshold:        <number>   (% for conversion/exception, ms for duration)
        query_duration:   <int>      minutes — width of the query window
        index:            funnel     (required by loader schema but unused)

    Pipeline/branch rules additionally require:
        lookback_minutes: <int>  minutes — root stage window ends this far before t1;
                                 other stages still extend to t1.
    """

    required_options = frozenset([
        'funnel_api_url',
        'query_payload',
        'threshold',
        'query_duration',
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
        duration = datetime.timedelta(minutes=rule['query_duration'])

        if self.alert_template in _PIPELINE_BRANCH_TEMPLATES:
            lookback = datetime.timedelta(minutes=rule['lookback_minutes'])
            # Root stage: [t1 - lookback - duration, t1 - lookback]
            # Other stages: [t1 - lookback - duration, t1]
            start_time = endtime - lookback - duration
            end_time = endtime
            time_buffer_minutes = rule['lookback_minutes']
        else:
            # Stage alerts: flat window [t1 - duration, t1], no offset.
            start_time = endtime - duration
            end_time = endtime
            time_buffer_minutes = None

        parameters = {
            'start_time': _fmt_ts(start_time),
            'end_time': _fmt_ts(end_time),
            **rule['query_payload'],
        }
        if time_buffer_minutes is not None:
            parameters['time_buffer_minutes'] = time_buffer_minutes

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

        self._check_threshold(rule, resp.json())

    def _check_threshold(self, rule, data):
        raise NotImplementedError

    def get_match_str(self, match):
        lines = ['Alert type: %s' % match.get('alert_type', self.alert_template)]
        for key, val in match.items():
            if key not in ('alert_type', 'num_hits', 'num_matches'):
                lines.append('  %s: %s' % (key, val))
        return '\n'.join(lines)


class PipelineConversionRateRule(FunnelAPIRuleType):
    """Alert when the minimum conversion rate across all pipeline leaf stages drops below threshold (%).

    Uses the full pipeline stage graph in query_payload.
    Fires when min(leaf_spans / root_spans * 100) < threshold.
    """

    alert_template = 'pipeline_conversion_rate'
    required_options = FunnelAPIRuleType.required_options | frozenset(['lookback_minutes'])

    def _check_threshold(self, rule, data):
        min_rate = data.get('min_conversion_rate', 100.0)
        if min_rate < rule['threshold']:
            self.add_match({
                'alert_type': 'pipeline_conversion_rate',
                'min_conversion_rate': min_rate,
                'threshold': rule['threshold'],
                'leaf_conversion_rates': data.get('leaf_conversion_rates', []),
            })


class BranchConversionRateRule(FunnelAPIRuleType):
    """Alert when conversion rate from root to the end of a branch drops below threshold (%).

    query_payload should define a linear path (one leaf stage).
    Fires when leaf_spans / root_spans * 100 < threshold.
    """

    alert_template = 'branch_conversion_rate'
    required_options = FunnelAPIRuleType.required_options | frozenset(['lookback_minutes'])

    def _check_threshold(self, rule, data):
        rate = data.get('conversion_rate', 100.0)
        if rate < rule['threshold']:
            self.add_match({
                'alert_type': 'branch_conversion_rate',
                'conversion_rate': rate,
                'threshold': rule['threshold'],
            })


class PipelineDurationRule(FunnelAPIRuleType):
    """Alert when the p95 end-to-end latency across all pipeline traces exceeds threshold (ms).

    p95 is computed over all traces regardless of which branch they took.
    Uses the full pipeline stage graph in query_payload.
    """

    alert_template = 'pipeline_duration'
    required_options = FunnelAPIRuleType.required_options | frozenset(['lookback_minutes'])

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_e2e_latency_ms', 0.0)
        if p95 > rule['threshold']:
            self.add_match({
                'alert_type': 'pipeline_duration',
                'p95_e2e_latency_ms': p95,
                'threshold': rule['threshold'],
            })


class BranchDurationRule(FunnelAPIRuleType):
    """Alert when the p95 end-to-end latency from root to the branch leaf exceeds threshold (ms).

    query_payload should define a linear path (one leaf stage).
    """

    alert_template = 'branch_duration'
    required_options = FunnelAPIRuleType.required_options | frozenset(['lookback_minutes'])

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_e2e_latency_ms', 0.0)
        if p95 > rule['threshold']:
            self.add_match({
                'alert_type': 'branch_duration',
                'p95_e2e_latency_ms': p95,
                'threshold': rule['threshold'],
            })


class StageDurationRule(FunnelAPIRuleType):
    """Alert when the p95 span duration of a single stage exceeds threshold (ms).

    query_payload should define exactly one stage.
    Duration is the span's own duration — not end-to-end from root.
    No lookback offset: window is [t1 - query_duration, t1].
    """

    alert_template = 'stage_duration'

    def _check_threshold(self, rule, data):
        p95 = data.get('p95_latency_ms', 0.0)
        if p95 > rule['threshold']:
            self.add_match({
                'alert_type': 'stage_duration',
                'stage_id': data.get('stage_id'),
                'p95_latency_ms': p95,
                'threshold': rule['threshold'],
            })


class StageExceptionRateRule(FunnelAPIRuleType):
    """Alert when the exception rate (error_count / span_count * 100) of a stage exceeds threshold (%).

    query_payload should define exactly one stage.
    No lookback offset: window is [t1 - query_duration, t1].
    """

    alert_template = 'exception_rate'

    def _check_threshold(self, rule, data):
        rate = data.get('exception_rate', 0.0)
        if rate > rule['threshold']:
            self.add_match({
                'alert_type': 'exception_rate',
                'stage_id': data.get('stage_id'),
                'exception_rate': rate,
                'error_count': data.get('error_count'),
                'span_count': data.get('span_count'),
                'threshold': rule['threshold'],
            })
