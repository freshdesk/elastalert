import prometheus_client
from elastalert.util import EAException
from elastalert.util import elastalert_logger


# Initialize global exception metric at module level
# This metric can be used even before PrometheusWrapper is instantiated
# (e.g., in loaders.py during ElastAlerter initialization)


# Module-level reference to the metric (set when PrometheusWrapper is instantiated)
# This allows the static method to access the metric
elastalert_load_rule_failed_total = None

elastalert_exceptions_total = prometheus_client.Counter(
        'elastalert_exceptions_total',
        'Total number of all unhandled exceptions in ElastAlert',
        ['error_type', 'error_message']
)


# elastalert_unhandled_exceptions_total = prometheus_client.Counter(
#         'elastalert_unhandled_exceptions_total',
#         'Total number of all unhandled exceptions in ElastAlert',
#         ['rule', 'tenant', 'error_type']
# )




class PrometheusWrapper:
    """ Exposes ElastAlert metrics on a Prometheus metrics endpoint.
        Wraps ElastAlerter run_rule and writeback to collect metrics. """

    def __init__(self, client):
        self.prometheus_port = client.prometheus_port
        self.run_rule = client.run_rule
        self.writeback = client.writeback
        self.handle_uncaught_exception = client.handle_uncaught_exception

        client.run_rule = self.metrics_run_rule
        client.writeback = self.metrics_writeback
        client.handle_uncaught_exception = self.metrics_handle_uncaught_exception


        # initialize prometheus metrics to be exposed
        self.prom_scrapes = prometheus_client.Counter('elastalert_scrapes', 'Number of scrapes for rule', ['rule_name'])
        self.prom_hits = prometheus_client.Counter('elastalert_hits', 'Number of hits for rule', ['rule_name'])
        self.prom_matches = prometheus_client.Counter('elastalert_matches', 'Number of matches for rule', ['rule_name'])
        self.prom_time_taken = prometheus_client.Counter('elastalert_time_taken', 'Time taken to evaluate rule', ['rule_name'])
        self.prom_alerts_sent = prometheus_client.Counter('elastalert_alerts_sent', 'Number of alerts sent for rule', ['rule_name'])
        self.prom_alerts_not_sent = prometheus_client.Counter('elastalert_alerts_not_sent', 'Number of alerts not sent', ['rule_name'])
        self.prom_errors = prometheus_client.Counter('elastalert_errors', 'Number of errors for rule')
        self.prom_alerts_silenced = prometheus_client.Counter('elastalert_alerts_silenced', 'Number of silenced alerts', ['rule_name'])
        # Initialize prometheus metrics similar to other metrics above
        self.elastalert_load_rule_failed_total = prometheus_client.Counter('elastalert_load_rule_failed_total','Total number of exceptions encountered while loading rule files',['rule', 'tenant', 'error_type'])
        # Reference to module-level metric for consistency with self.metricname pattern
        self.elastalert_exceptions_total = elastalert_exceptions_total
        self.elastalert_unhandled_exceptions_total = prometheus_client.Counter('elastalert_unhandled_exceptions_total','Total number of all unhandled exceptions in ElastAlert',['rule', 'tenant', 'error_type'])
        
        # Set module-level reference so static method can use it
        global elastalert_load_rule_failed_total
        elastalert_load_rule_failed_total = self.elastalert_load_rule_failed_total

    def start(self):
        prometheus_client.start_http_server(self.prometheus_port)

    def metrics_run_rule(self, rule, endtime, starttime=None):
        """ Increment counter every time rule is run """
        try:
            self.prom_scrapes.labels(rule['name']).inc()
        finally:
            return self.run_rule(rule, endtime, starttime)

    def metrics_writeback(self, doc_type, body, rule=None, match_body=None):
        """ Update various prometheus metrics accoording to the doc_type """

        res = self.writeback(doc_type, body)
        try:
            if doc_type == 'elastalert_status':
                self.prom_hits.labels(body['rule_name']).inc(int(body['hits']))
                self.prom_matches.labels(body['rule_name']).inc(int(body['matches']))
                self.prom_time_taken.labels(body['rule_name']).inc(float(body['time_taken']))
            elif doc_type == 'elastalert':
                if body['alert_sent']:
                    self.prom_alerts_sent.labels(body['rule_name']).inc()
                else:
                    self.prom_alerts_not_sent.labels(body['rule_name']).inc()
            elif doc_type == 'elastalert_error':
                print("coming_here")
                print(body)
                print("pt 2")
                self.prom_errors.inc()
            elif doc_type == 'silence':
                self.prom_alerts_silenced.labels(body['rule_name']).inc()
        finally:
            return res

    def metrics_handle_uncaught_exception(self, exception, rule):
        """ Increment counter every time rule is run """
        print("\ncoming_here wrapper :: 11111111\n")
        try:
            rule_name = rule.get('name') or 'unknown'
            tenant = "unknown"
            if rule_name != 'unknown':
                tenant = rule_name.split('_')[0]
            error_type = exception.__class__.__name__
            self.elastalert_unhandled_exceptions_total.labels(rule=rule_name, tenant=tenant, error_type=error_type).inc()
        finally:
            return self.handle_uncaught_exception(exception, rule)


    @staticmethod
    def increment_load_rule_failed_total(rule, e):
        """Static method to increment the load rule failed metric.
        Can be called as PrometheusWrapper.increment_load_rule_failed_total() without an instance."""
        elastalert_logger.error("########################increment_load_rule_failed_total########################")
        # Handle case where rule might be None (if load_configuration failed)
        rule_name = 'unknown'
        if rule is not None:
            rule_name = rule.get('name') or 'unknown'
        
        tenant = "unknown"
        if rule_name != 'unknown':
            tenant = rule_name.split('_')[0]
        error_type = e.__class__.__name__

        if elastalert_load_rule_failed_total is not None:
            elastalert_logger.error("########################elastalert_load_rule_failed_total is not None########################")
            elastalert_load_rule_failed_total.labels(rule=rule_name, tenant=tenant, error_type=error_type).inc()
        else:
            elastalert_logger.error("########################elastalert_load_rule_failed_total is None ########################")
