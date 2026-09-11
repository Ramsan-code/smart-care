from rest_framework import serializers
from rest_framework.response import Response

from scheduling.api import DomainView, StrictInput
from .services import retry_delivery, scoped_deliveries, delivery_data


class RetryInput(StrictInput):
    expected_retry_count = serializers.IntegerField(min_value=0)


class DeliveryListView(DomainView):
    def get(self, request):
        qs = scoped_deliveries(request.user)
        return Response({"results": [delivery_data(item) for item in qs.order_by("-created_at")[:100]]})


class DeliveryRetryView(DomainView):
    def post(self, request, pk):
        form = RetryInput(data=request.data)
        form.is_valid(raise_exception=True)
        delivery = scoped_deliveries(request.user).filter(pk=pk).first()
        if not delivery:
            from scheduling.services import BookingError
            raise BookingError("not_found", "Notification delivery not found.", 404)
        if delivery.retry_count != form.validated_data["expected_retry_count"]:
            from scheduling.services import BookingError
            raise BookingError("stale_version", "This delivery has changed. Refresh before retrying.")
        return Response(retry_delivery(request.user, pk))
