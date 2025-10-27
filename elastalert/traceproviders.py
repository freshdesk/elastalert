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

config = {
    'enabled': True,
    'otel_exporter_endpoint': 'trace-shipper.trace-shipper:55680',
    'otel_sdk_version': '1.21.0',
    'trace_service_name': 'elastalert',
    'trace_sampling_probability': 1.0
}
telemetry_sdk_name = "opentelemetry"
telemetry_sdk_language = "python"
telemetry_sdk_version = "1.25.0"

def get_hostname():
    return socket.gethostname()

def get_host_ip():
    return socket.gethostbyname(socket.gethostname())

def init_tracer():
    print("Inside init_tracer")
    try:
        # Create resource with service information
        resource = Resource.create({
            ResourceAttributes.SERVICE_NAME: config.get('trace_service_name'),
            ResourceAttributes.TELEMETRY_SDK_NAME: telemetry_sdk_name,
            ResourceAttributes.TELEMETRY_SDK_LANGUAGE: "python",
            ResourceAttributes.TELEMETRY_SDK_VERSION: config.get('otel_sdk_version', '1.25.0'),
            ResourceAttributes.HOST_NAME: get_hostname(),
            ResourceAttributes.HOST_ID: get_host_ip(),  # Using HOST_ID for IP address
        })

        # Create OTLP exporter
        otlp_exporter = OTLPSpanExporter(
            endpoint=config.get('otel_exporter_endpoint', 'http://localhost:4317'),
            insecure=True  # Note: Use secure connections in production
        )

        # Create tracer provider with sampling
        trace_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(
                root=TraceIdRatioBased(
                    config.get('trace_sampling_probability', 1.0)
                )
            )
        )
        logging.getLogger('elastalert').info("OpenTelemetry tracing initialized successfully")

        span_processor = BatchSpanProcessor(otlp_exporter)
        trace_provider.add_span_processor(span_processor)

        # Set global tracer provider
        trace.set_tracer_provider(trace_provider)

        # Create tracer instance
        tracer = trace.get_tracer("elastalert-service")

        logging.getLogger('elastalert').info("OpenTelemetry tracing initialized successfully")

    except Exception as e:
        logging.getLogger('elastalert').error(f"Failed to initialize tracing: {e}")
    

    return trace_provider