from opentelemetry import trace, context, propagate
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, Span
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace import SpanKind

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
import socket
import logging
from functools import wraps

from opentelemetry.instrumentation.requests import RequestsInstrumentor


def get_hostname():
    return socket.gethostname()

def get_host_ip():
    return socket.gethostbyname(socket.gethostname())

def init_tracer(config):
    logger = logging.getLogger('elastalert')
    
    # Check if tracing is enabled
    if not config.get('enabled', True):
        logger.info("Tracing is disabled in configuration")
        return None
    

    tracer = None
    try:
        endpoint = config.get('otel_exporter_endpoint', 'trace-shipper.trace-shipper:55680')
        logger.info(f"Initializing OpenTelemetry tracer with endpoint: {endpoint}")
        logger.info(f"Trace service name: {config.get('trace_service_name', 'elastalert')}")
        logger.info(f"service name form config: {config.get('trace_service_name')}")
        
        # Create resource with service information
        resource = Resource.create({
            ResourceAttributes.SERVICE_NAME: config.get('trace_service_name', 'elastalert'),
            ResourceAttributes.TELEMETRY_SDK_NAME: config.get('telemetry_sdk_name', 'opentelemetry'),
            ResourceAttributes.TELEMETRY_SDK_LANGUAGE: "python",
            ResourceAttributes.TELEMETRY_SDK_VERSION: config.get('otel_sdk_version', '1.25.0'),
            ResourceAttributes.HOST_NAME: get_hostname(),
            ResourceAttributes.HOST_ID: get_host_ip(),  # Using HOST_ID for IP address
        })


        trace_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(
                root=TraceIdRatioBased(
                    config.get('trace_sampling_probability', 1.0)
                )
            )
        )

        # Create OTLP exporter
        otlp_exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=True
        )

        span_processor = BatchSpanProcessor(otlp_exporter)


        trace_provider.add_span_processor(span_processor)

        trace.set_tracer_provider(trace_provider)

        # Initialize requests auto-instrumentation
        try:
            RequestsInstrumentor().instrument(tracer_provider=trace_provider)
            logger.info("Requests auto-instrumentation initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize requests auto-instrumentation: {e}")

        tracer = trace.get_tracer("elastalert-service")


        return tracer



        # # Set global tracer provider
        # logger.info("Setting global tracer provider")

        # # Create tracer instance
        # logger.info("Creating tracer instance")
        # tracer = trace.get_tracer("elastalert-service")

        # logger.info("OpenTelemetry tracing initialized successfully with endpoint: %s" % endpoint)

    except Exception as e:
        logging.getLogger('elastalert').error(f"Failed to initialize tracing: {e}")
        import traceback
        logging.getLogger('elastalert').error(traceback.format_exc())
        tracer = None
    

    return tracer


def trace_span(span_name):
    """
    Decorator to automatically create a span for a method (works with both instance and static methods).
    
    This decorator ensures proper span hierarchy:
    - Spans created here automatically become CHILD spans of any active parent span
    - Uses start_as_current_span which inherits the current span context
    - Works across different modules (elastalert.py, ruletypes.py, etc.)
    
    Usage:
        # For instance methods:
        @trace_span("method.name")
        def instance_method(self, ...):
            ...
        
        # For static methods (apply trace_span BEFORE @staticmethod):
        @trace_span("method.name")
        @staticmethod
        def static_method(...):
            ...
    
    Args:
        span_name: Name of the span to create (e.g., "elastalert.run_query")
    
    Returns:
        Decorated function with automatic span creation and error handling
    """
    def decorator(func):
        # Handle staticmethod objects - extract the underlying function
        if isinstance(func, staticmethod):
            original_func = func.__func__
            is_static = True
        else:
            original_func = func
            is_static = False
        
        @wraps(original_func)
        def wrapper(*args, **kwargs):
            # Get tracer - uses the same TracerProvider for all modules
            tracer = trace.get_tracer("elastalert-service")
            
            # start_as_current_span automatically inherits the current span context
            # If called from within an existing span, this becomes a child span
            with tracer.start_as_current_span(span_name) as span:
                try:
                    # Set method name attribute
                    span.set_attribute("method.name", original_func.__name__)
                    
                    # Try to get rule name from various sources
                    rule_name = None
                    
                    # First, try to get from thread_data (set in run_rule for elastalert.py)
                    if not is_static and args:
                        self_obj = args[0]
                        if hasattr(self_obj, 'thread_data'):
                            try:
                                rule_name = getattr(self_obj.thread_data, 'current_rule_name', None)
                            except (AttributeError, RuntimeError):
                                pass
                        
                        # For ruletypes, try to get from self.rules
                        if not rule_name and hasattr(self_obj, 'rules') and isinstance(self_obj.rules, dict):
                            rule_name = self_obj.rules.get('name', None)
                    
                    # Second, try to find rule in method arguments
                    if not rule_name:
                        for arg in args:
                            if isinstance(arg, dict) and 'name' in arg:
                                rule_name = arg.get('name', 'unknown')
                                break
                    
                    # Add rule name to span if found
                    if rule_name:
                        span.set_attribute("rule.name", rule_name)
                    
                    # Set class name if this is an instance method or has an object as first parameter
                    if args:
                        first_arg = args[0]
                        if hasattr(first_arg, '__class__'):
                            class_name = first_arg.__class__.__name__
                            # Only set class name if it looks like a method's self parameter
                            if not is_static and hasattr(first_arg, original_func.__name__):
                                span.set_attribute("method.class", class_name)
                            elif hasattr(first_arg, 'rules'):  # Likely a rule type instance
                                span.set_attribute("method.class", class_name)
                    
                    return original_func(*args, **kwargs)
                except Exception as e:
                    # Record error on span with detailed exception information
                    span.record_exception(e, escaped=True)
                    # Set status to ERROR - this must be done before span ends
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    # Also set error attributes directly on the span for visibility
                    span.set_attribute("error", True)
                    span.set_attribute("exception.type", e.__class__.__name__)
                    span.set_attribute("exception.message", str(e))
                    # Re-raise the exception to maintain normal error flow
                    raise
        
        # If the original func was a staticmethod, return a staticmethod-wrapped wrapper
        if is_static:
            return staticmethod(wrapper)
        return wrapper
    return decorator


# ============================================================================
# Span Helper Functions
# ============================================================================

def get_recording_span():
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        return current_span
    return None