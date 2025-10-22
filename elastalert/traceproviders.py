# -*- coding: utf-8 -*-
"""
OpenTelemetry tracing providers for ElastAlert.

This module provides a unified interface for distributed tracing in ElastAlert,
following the same patterns as haystack-router with proper context propagation.
"""

import logging
import socket
from typing import Optional, Dict, Any, Callable, Tuple
from contextvars import ContextVar
import contextvars

from opentelemetry import trace, context, propagate
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, Span
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status, StatusCode

# Global tracer and provider instances
tracer: Optional[trace.Tracer] = None
trace_provider: Optional[TracerProvider] = None
telemetry_sdk_name = "opentelemetry"


# Attribute class (matching haystack-router pattern) - must be defined early
class Attribute:
    """
    Attribute wrapper class matching haystack-router's Attribute struct.
    """
    def __init__(self, key: str, value: Any):
        self.key = key
        self.value = value
    
    def get_attribute(self) -> tuple:
        """Get attribute as tuple for OpenTelemetry."""
        return (self.key, self.value)


def init_tracer(config: Dict[str, Any]) -> Optional[Callable]:
    """
    Initialize the OpenTelemetry tracer with the given configuration.
    
    Args:
        config: Dictionary containing tracing configuration with keys (matching haystack-router):
            - otel_exporter_endpoint: OTLP gRPC endpoint (TraceOTELExporterEndpoint)
            - otel_sdk_version: OpenTelemetry SDK version (TraceOTELSDKVersion)
            - trace_service_name: Service name for traces (TraceServiceName)
            - trace_sampling_probability: Sampling probability (TraceSamplingProbability)
    
    Returns:
        Shutdown function for graceful cleanup, or None if initialization failed
    """
    global tracer, trace_provider
    
    if trace_provider is not None:
        return trace_provider.shutdown
    
    try:
        # Create resource with service information (matching haystack-router exactly)
        resource_attrs = {
            ResourceAttributes.SERVICE_NAME: config.get('trace_service_name', 'elastalert'),
            ResourceAttributes.TELEMETRY_SDK_NAME: telemetry_sdk_name,
            ResourceAttributes.TELEMETRY_SDK_LANGUAGE: "python", 
            ResourceAttributes.TELEMETRY_SDK_VERSION: config.get('otel_sdk_version', '1.25.0'),
            ResourceAttributes.HOST_NAME: get_hostname(),
        }
        
        # Add server address (use different attribute names depending on OpenTelemetry version)
        try:
            resource_attrs[ResourceAttributes.SERVER_ADDRESS] = get_host_ip()
        except AttributeError:
            # Fallback for older versions - use a custom attribute
            resource_attrs["server.address"] = get_host_ip()
        
        resource = Resource.create(resource_attrs)
        
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
        
        # Add span processor
        span_processor = BatchSpanProcessor(otlp_exporter)
        trace_provider.add_span_processor(span_processor)
        
        # Set global tracer provider
        trace.set_tracer_provider(trace_provider)
        
        # Set global propagator to tracecontext (matching haystack-router exactly)
        try:
            from opentelemetry.propagators.composite import CompositeHTTPPropagator
            from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator as TCPropagator
            from opentelemetry.propagators.baggage import BaggagePropagator
            
            propagate.set_global_textmap(CompositeHTTPPropagator([
                TCPropagator(),
                BaggagePropagator()
            ]))
        except ImportError:
            # Fallback for different OpenTelemetry versions
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            from opentelemetry.baggage.propagation import W3CBaggagePropagator
            from opentelemetry.propagators.composite import CompositePropagator
            
            propagate.set_global_textmap(CompositePropagator([
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator()
            ]))
        
        # Create tracer instance (matching haystack-router pattern)
        tracer = trace_provider.get_tracer("elastalert-service")
        
        logging.getLogger('elastalert').info("OpenTelemetry tracing initialized successfully")
        
        return trace_provider.shutdown
        
    except Exception as e:
        logging.getLogger('elastalert').error(f"Failed to initialize tracing: {e}")
        return None


def create_span(ctx: Optional[Any], name: str, attributes: Optional[Dict[str, Any]] = None) -> Tuple[Any, trace.Span]:
    """
    Create a new span with the given name and attributes, following haystack-router pattern.
    Matches: CreateSpan(ctx context.Context, name string, attributes ...Attribute) (context.Context, trace.Span)
    
    Args:
        ctx: Parent context (can be None) - should contain parent span for proper parent-child relationships
        name: Name of the span
        attributes: Optional dictionary of span attributes
    
    Returns:
        Tuple of (new_context, span) - new context with span and the span object
    """
    if tracer is None:
        # Return a no-op span if tracer is not initialized
        noop_span = trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False
        ))
        return ctx, noop_span
    
    try:
        # Create span with proper parent-child relationship
        # If ctx is None, create background context
        if ctx is None:
            # Create span without parent (root span)
            span = tracer.start_span(name)
        else:
            # Create child span from parent context - THIS IS THE KEY!
            span = tracer.start_span(name, context=ctx)
        
        # Set attributes if provided (supporting both dict and Attribute patterns)
        if attributes:
            if isinstance(attributes, dict):
                # Dictionary format for backward compatibility
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            else:
                # Assume it's iterable of Attribute objects (haystack-router style)
                try:
                    trace_attributes = _convert_to_trace_attributes(*attributes)
                    for key, value in trace_attributes:
                        span.set_attribute(key, value)
                except:
                    # Fallback to dict format
                    for key, value in attributes.items():
                        span.set_attribute(key, value)
        
        # Create new context with this span (for passing to child operations)
        new_ctx = trace.set_span_in_context(span, ctx or context.Context())
        
        return new_ctx, span
        
    except Exception as e:
        logging.getLogger('elastalert').warning(f"Failed to create span {name}: {e}")
        noop_span = trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False
        ))
        return ctx, noop_span


def context_with_span(span: trace.Span) -> Any:
    """
    Create a context with the given span (for scatter-gather operations).
    Matches: ContextWithSpan(span trace.Span) context.Context
    
    Args:
        span: Span to embed in context
    
    Returns:
        New context with the span
    """
    # Create fresh context with the span (like haystack-router does)
    return trace.set_span_in_context(span, context.Context())


def get_span_from_context(ctx: Optional[Any]) -> trace.Span:
    """
    Get the current span from the context.
    Matches: GetSpanFromContext(ctx context.Context) trace.Span
    
    Args:
        ctx: Context to get span from
    
    Returns:
        Current span or no-op span if none found
    """
    if ctx is None:
        return trace.get_current_span()
    
    # Get span from the provided context
    return trace.get_current_span(ctx)


def end_span(span: trace.Span):
    """
    End the given span.
    
    Args:
        span: Span to end
    """
    if span:
        span.end()


def record_error(span: trace.Span, error: Exception):
    """
    Record an error on the given span.
    
    Args:
        span: Span to record error on
        error: Exception to record
    """
    if span and error:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))


def add_event(span: trace.Span, name: str, *attributes: Attribute):
    """
    Add an event to the given span (matching haystack-router AddEvent signature).
    Matches: AddEvent(span trace.Span, name string, attributes ...Attribute)
    
    Args:
        span: Span to add event to
        name: Name of the event
        attributes: Variable number of Attribute objects
    """
    if span:
        trace_attributes = _convert_to_trace_attributes(*attributes)
        span.add_event(name, dict(trace_attributes))


def set_attributes(span: trace.Span, *attributes: Attribute):
    """
    Set attributes on the given span (matching haystack-router SetAttributes signature).
    Matches: SetAttributes(span trace.Span, attributes ...Attribute)
    
    Args:
        span: Span to set attributes on
        attributes: Variable number of Attribute objects
    """
    if span and attributes:
        trace_attributes = _convert_to_trace_attributes(*attributes)
        for key, value in trace_attributes:
            span.set_attribute(key, value)


def get_hostname() -> str:
    """
    Get the hostname of the current machine.
    
    Returns:
        Hostname string
    """
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def get_host_ip() -> str:
    """
    Get the IP address of the current machine.
    
    Returns:
        IP address string
    """
    try:
        # Connect to a dummy address to find the local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class TraceContextManager:
    """
    Context manager for creating and managing spans with automatic cleanup.
    Similar to haystack-router pattern but with Python context managers.
    """
    
    def __init__(self, ctx: Optional[Any], name: str, attributes: Optional[Dict[str, Any]] = None):
        self.ctx = ctx
        self.name = name
        self.attributes = attributes or {}
        self.span: Optional[trace.Span] = None
        self.new_ctx: Optional[Any] = None
    
    def __enter__(self) -> Tuple[Any, trace.Span]:
        self.new_ctx, self.span = create_span(self.ctx, self.name, self.attributes)
        return self.new_ctx, self.span
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            record_error(self.span, exc_val)
        end_span(self.span)


def trace_operation(name: str, attributes: Optional[Dict[str, Any]] = None):
    """
    Decorator for tracing function operations with context propagation.
    
    Args:
        name: Name of the span
        attributes: Optional span attributes
    
    Returns:
        Decorated function
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Try to get context from args (if available)
            ctx = None
            if args and hasattr(args[0], '_trace_context'):
                ctx = getattr(args[0], '_trace_context', None)
                
            with TraceContextManager(ctx, name, attributes) as (new_ctx, span):
                set_attributes(span, {
                    'function.name': func.__name__,
                    'function.module': func.__module__,
                })
                
                # If first argument has context attribute, update it
                if args and hasattr(args[0], '_trace_context'):
                    setattr(args[0], '_trace_context', new_ctx)
                
                return func(*args, **kwargs)
        return wrapper
    return decorator


# Attribute helper functions (exactly matching haystack-router pattern)


def string_attribute(key: str, value: str) -> Attribute:
    """Create a string attribute (matching haystack-router StringAttribute)."""
    return Attribute(key, value)


def int_attribute(key: str, value: int) -> Attribute:
    """Create an integer attribute (matching haystack-router IntAttribute)."""
    return Attribute(key, value)


def int64_attribute(key: str, value: int) -> Attribute:
    """Create an int64 attribute (matching haystack-router Int64Attribute)."""
    return Attribute(key, value)


def float64_attribute(key: str, value: float) -> Attribute:
    """Create a float64 attribute (matching haystack-router Float64Attribute)."""
    return Attribute(key, value)


def bool_attribute(key: str, value: bool) -> Attribute:
    """Create a boolean attribute (matching haystack-router BoolAttribute)."""
    return Attribute(key, value)


def _convert_to_trace_attributes(*attributes: Attribute) -> list:
    """
    Convert Attribute objects to OpenTelemetry format.
    Matches haystack-router's convertToTraceAttributes function.
    """
    trace_attributes = []
    for attr in attributes:
        if isinstance(attr, Attribute):
            trace_attributes.append(attr.get_attribute())
    return trace_attributes
