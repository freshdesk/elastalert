"""
OpenTelemetry tracing providers for ElastAlert.

This module provides a unified interface for distributed tracing in ElastAlert,
following proper context propagation patterns.
"""

import logging
import socket
from typing import Optional, Dict, Any, Callable, Tuple
import functools

from opentelemetry import trace, context, propagate
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, Span
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace import SpanKind

# Global tracer and provider instances
tracer: Optional[trace.Tracer] = None
trace_provider: Optional[TracerProvider] = None
telemetry_sdk_name = "opentelemetry"


# Attribute class (matching haystack-router pattern)
class Attribute:
    """
    Attribute wrapper class for OpenTelemetry attributes.
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
        config: Dictionary containing tracing configuration with keys:
            - otel_exporter_endpoint: OTLP gRPC endpoint
            - otel_sdk_version: OpenTelemetry SDK version
            - trace_service_name: Service name for traces
            - trace_sampling_probability: Sampling probability
    
    Returns:
        Shutdown function for graceful cleanup, or None if initialization failed
    """
    global tracer, trace_provider
    
    if trace_provider is not None:
        return trace_provider.shutdown
    
    try:
        # Create resource with service information
        resource_attrs = {
            ResourceAttributes.SERVICE_NAME: config.get('trace_service_name', 'elastalert'),
            ResourceAttributes.TELEMETRY_SDK_NAME: telemetry_sdk_name,
            ResourceAttributes.TELEMETRY_SDK_LANGUAGE: "python", 
            ResourceAttributes.TELEMETRY_SDK_VERSION: config.get('otel_sdk_version', '1.25.0'),
            ResourceAttributes.HOST_NAME: get_hostname(),
        }
        
        # Add server address
        try:
            resource_attrs[ResourceAttributes.SERVER_ADDRESS] = get_host_ip()
        except AttributeError:
            resource_attrs["server.address"] = get_host_ip()
        
        resource = Resource.create(resource_attrs)
        
        # Create OTLP exporter
        otlp_exporter = OTLPSpanExporter(
            endpoint=config.get('otel_exporter_endpoint', 'http://localhost:4317'),
            insecure=True
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
        
        # Set global propagator for context propagation
        try:
            from opentelemetry.propagators.composite import CompositeHTTPPropagator
            from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator as TCPropagator
            from opentelemetry.propagators.baggage import BaggagePropagator
            
            propagate.set_global_textmap(CompositeHTTPPropagator([
                TCPropagator(),
                BaggagePropagator()
            ]))
        except ImportError:
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            from opentelemetry.baggage.propagation import W3CBaggagePropagator
            from opentelemetry.propagators.composite import CompositePropagator
            
            propagate.set_global_textmap(CompositePropagator([
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator()
            ]))
        
        # Create tracer instance
        tracer = trace_provider.get_tracer("elastalert-service")
        
        logging.getLogger('elastalert').info("OpenTelemetry tracing initialized successfully")
        
        return trace_provider.shutdown
        
    except Exception as e:
        logging.getLogger('elastalert').error(f"Failed to initialize tracing: {e}")
        return None


def create_span(ctx: Optional[Any], name: str, attributes: Optional[Dict[str, Any]] = None) -> Tuple[Any, trace.Span]:
    """
    Create a new span with the given name and attributes, with proper context propagation.
    
    Args:
        ctx: Parent context (can be None) - should contain parent span for proper parent-child relationships
        name: Name of the span
        attributes: Dictionary of span attributes
    
    Returns:
        Tuple of (new_context, span) - new context with span and the span object
    """
    if tracer is None:
        # Return a no-op span if tracer is not initialized
        noop_span = trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False,
            trace_flags=trace.TraceFlags(0)
        ))
        return ctx, noop_span
    
    try:
        # Create span with proper parent-child relationship
        if ctx is None:
            # Create root span with background context
            span = tracer.start_span(name, kind=SpanKind.INTERNAL)
        else:
            # Create child span from parent context - KEY FOR CONTEXT PROPAGATION
            span = tracer.start_span(name, context=ctx, kind=SpanKind.INTERNAL)
        
        # Set attributes if provided
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, str(value))
        
        # Create new context with this span (for passing to child operations)
        new_ctx = trace.set_span_in_context(span, ctx if ctx is not None else context.get_current())
        
        return new_ctx, span
        
    except Exception as e:
        logging.getLogger('elastalert').warning(f"Failed to create span {name}: {e}")
        noop_span = trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False,
            trace_flags=trace.TraceFlags(0)
        ))
        return ctx, noop_span


def context_with_span(span: trace.Span) -> Any:
    """
    Create a context with the given span.
    
    Args:
        span: Span to embed in context
    
    Returns:
        New context with the span
    """
    return trace.set_span_in_context(span, context.get_current())


def get_span_from_context(ctx: Optional[Any]) -> trace.Span:
    """
    Get the current span from the context.
    
    Args:
        ctx: Context to get span from
    
    Returns:
        Current span or no-op span if none found
    """
    if ctx is None:
        return trace.get_current_span()
    
    return trace.get_current_span(ctx)


def end_span(span: trace.Span):
    """
    End the given span.
    
    Args:
        span: Span to end
    """
    if span and span.is_recording():
        span.end()


def record_error(span: trace.Span, error: Exception):
    """
    Record an error on the given span.
    
    Args:
        span: Span to record error on
        error: Exception to record
    """
    if span and span.is_recording() and error:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))


def add_event(span: trace.Span, name: str, *attributes: Attribute):
    """
    Add an event to the given span.
    
    Args:
        span: Span to add event to
        name: Name of the event
        attributes: Variable number of Attribute objects
    """
    if span and span.is_recording():
        trace_attributes = _convert_to_trace_attributes(*attributes)
        span.add_event(name, dict(trace_attributes))


def set_attributes(span: trace.Span, attributes: Optional[Dict[str, Any]] = None, **kwargs):
    """
    Set attributes on the given span.
    
    Args:
        span: Span to set attributes on
        attributes: Dictionary of attributes to set
        **kwargs: Additional key-value pairs for attributes
    """
    if not span or not span.is_recording():
        return
    
    # Handle dictionary attributes
    if attributes:
        for key, value in attributes.items():
            span.set_attribute(key, str(value))
    
    # Handle keyword arguments
    if kwargs:
        for key, value in kwargs.items():
            span.set_attribute(key, str(value))


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
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class TraceContextManager:
    """
    Context manager for creating and managing spans with automatic cleanup.
    """
    
    def __init__(self, ctx: Optional[Any], name: str, **attributes):
        self.ctx = ctx
        self.name = name
        self.attributes = attributes
        self.span: Optional[trace.Span] = None
        self.new_ctx: Optional[Any] = None
    
    def __enter__(self) -> Tuple[Any, trace.Span]:
        self.new_ctx, self.span = create_span(self.ctx, self.name, **self.attributes)
        return self.new_ctx, self.span
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            record_error(self.span, exc_val)
        end_span(self.span)


def trace_operation(name: str, **attributes):
    """
    Decorator for tracing function operations with context propagation.
    
    Args:
        name: Name of the span
        **attributes: Keyword arguments for span attributes (labels)
    
    Returns:
        Decorated function
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Try to get context from self._trace_context if available
            ctx = None
            if args and hasattr(args[0], '_trace_context'):
                ctx = getattr(args[0], '_trace_context', None)
                
            with TraceContextManager(ctx, name, **attributes) as (new_ctx, span):
                set_attributes(span, 
                    function_name=func.__name__,
                    function_module=func.__module__
                )
                
                # If first argument has context attribute, update it
                if args and hasattr(args[0], '_trace_context'):
                    setattr(args[0], '_trace_context', new_ctx)
                
                return func(*args, **kwargs)
        return wrapper
    return decorator


# Attribute helper functions

def string_attribute(key: str, value: str) -> Attribute:
    """Create a string attribute."""
    return Attribute(key, value)


def int_attribute(key: str, value: int) -> Attribute:
    """Create an integer attribute."""
    return Attribute(key, value)


def int64_attribute(key: str, value: int) -> Attribute:
    """Create an int64 attribute."""
    return Attribute(key, value)


def float64_attribute(key: str, value: float) -> Attribute:
    """Create a float64 attribute."""
    return Attribute(key, value)


def bool_attribute(key: str, value: bool) -> Attribute:
    """Create a boolean attribute."""
    return Attribute(key, value)


def _convert_to_trace_attributes(*attributes: Attribute) -> list:
    """
    Convert Attribute objects to OpenTelemetry format.
    """
    trace_attributes = []
    for attr in attributes:
        if isinstance(attr, Attribute):
            trace_attributes.append(attr.get_attribute())
    return trace_attributes


# Additional helper functions for custom tracing

def add_span_labels(**labels):
    """
    Add custom labels to the current active span.
    
    Args:
        **labels: Keyword arguments for labels
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        for key, value in labels.items():
            current_span.set_attribute(f"label.{key}", str(value))


def add_span_event_simple(event_name: str, **attributes):
    """
    Add an event to the current active span.
    
    Args:
        event_name: Name of the event
        **attributes: Event attributes
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.add_event(event_name, attributes or {})


def get_current_trace_info() -> Dict[str, str]:
    """
    Get current trace context information.
    
    Returns:
        Dictionary with trace_id, span_id, and other context info
    """
    current_span = trace.get_current_span()
    if not current_span:
        return {"error": "No current span available"}
    
    span_context = current_span.get_span_context()
    if not span_context.is_valid:
        return {"error": "No valid span context available"}
    
    return {
        "trace_id": f"{span_context.trace_id:032x}",
        "span_id": f"{span_context.span_id:016x}",
        "is_sampled": str(span_context.trace_flags & 1 == 1),  # Check sampled flag
        "is_remote": str(span_context.is_remote),
    }


def create_child_span_from_current(name: str, attributes: Optional[Dict[str, Any]] = None) -> Tuple[Any, trace.Span]:
    """
    Create a child span from the current active span context.
    Useful for operations within the same thread.
    
    Args:
        name: Name of the span
        attributes: Dictionary of span attributes
    
    Returns:
        Tuple of (new_context, span)
    """
    current_ctx = context.get_current()
    return create_span(current_ctx, name, attributes)


def trace_method(method_name: Optional[str] = None, **attributes):
    """
    Decorator to automatically trace method calls.
    
    Args:
        method_name: Optional custom name for the span (defaults to method name)
        **attributes: Additional attributes to add to the span
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            # Use method name if not provided
            span_name = method_name or f"{self.__class__.__name__}.{func.__name__}"
            
            # Get context from self if available
            ctx = getattr(self, '_trace_context', None)
            
            # Merge function info with custom attributes
            span_attributes = {
                'method.class': self.__class__.__name__,
                'method.function': func.__name__,
                **attributes
            }
            
            span_ctx, span = create_span(ctx, span_name, span_attributes)
            
            # Update instance context if it has one
            if hasattr(self, '_trace_context'):
                original_ctx = self._trace_context
                self._trace_context = span_ctx
            
            try:
                result = func(self, *args, **kwargs)
                
                # Record success
                set_attributes(span, {'method.status': 'success'})
                
                return result
            except Exception as e:
                # Record error
                record_error(span, e)
                set_attributes(span, {'method.status': 'error'})
                raise
            finally:
                # Restore original context if it was set
                if hasattr(self, '_trace_context'):
                    self._trace_context = original_ctx
                    
                end_span(span)
        
        return wrapper
    return decorator


def is_tracing_enabled() -> bool:
    """
    Check if tracing is currently enabled and functional.
    
    Returns:
        True if tracing is enabled, False otherwise
    """
    return tracer is not None and trace_provider is not None
