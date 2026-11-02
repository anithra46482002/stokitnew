from django.db import connection, transaction
from rest_framework.response import Response
from .delivery_auth import _ProtectedView

DELIVERY_BOY_ALLOWED_STATUSES = {"delivered", "picked_up", "cancelled", "failed"}
DELIVERY_BOY_CUSTOM_STATUS = "other"
MAX_CUSTOM_STATUS_LENGTH = 100

def _dict_rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def _assigned_order(order_id, delivery_boy_id, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    with connection.cursor() as cursor:
        cursor.execute("SELECT order_id, full_name, address, city, state, postal_code, country, phone, email, total_amount, order_date, status, delivery_boy_id, payment_method, shipping_charge, cod_charge FROM zg_checkout WHERE order_id = %s AND delivery_boy_id = %s" + suffix, [order_id, delivery_boy_id])
        rows = _dict_rows(cursor)
    return rows[0] if rows else None

def _order_payload(order):
    with connection.cursor() as cursor:
        cursor.execute("SELECT item_id, product_id, variant_id, product_name, variant_name, quantity, price, discount, total_price FROM zg_order_items WHERE order_id = %s ORDER BY item_id", [order["order_id"]])
        items = _dict_rows(cursor)
        cursor.execute("SELECT status, changed_at, message FROM zg_order_status_history WHERE order_id = %s ORDER BY changed_at DESC, id DESC", [order["order_id"]])
        history = _dict_rows(cursor)
    return {"order": order, "items": items, "item_count": len(items), "status_history": history}

class DeliveryAssignedOrdersView(_ProtectedView):
    def get(self, request):
        with connection.cursor() as cursor:
            cursor.execute("SELECT c.order_id, c.full_name, c.phone, c.address, c.city, c.state, c.postal_code, c.country, c.total_amount, c.order_date, c.status, c.payment_method, c.shipping_charge, c.cod_charge, (SELECT COUNT(*) FROM zg_order_items i WHERE i.order_id = c.order_id) AS item_count, (SELECT STRING_AGG(i.product_name || ' x' || i.quantity::text, ', ' ORDER BY i.item_id) FROM zg_order_items i WHERE i.order_id = c.order_id) AS product_summary FROM zg_checkout c WHERE c.delivery_boy_id = %s ORDER BY c.order_date DESC, c.order_id DESC", [request.user.pk])
            orders = _dict_rows(cursor)
        return Response({"count": len(orders), "orders": orders})

class DeliveryAssignedOrderDetailView(_ProtectedView):
    def get(self, request, order_id):
        order = _assigned_order(order_id, request.user.pk)
        if order is None:
            return Response({"detail": "Assigned order not found."}, status=404)
        return Response(_order_payload(order))

class DeliveryAssignedOrderStatusView(_ProtectedView):
    def post(self, request, order_id):
        requested_status = str(request.data.get("status", "")).strip().lower()
        description = str(request.data.get("description", "") or "").strip()
        if requested_status == DELIVERY_BOY_CUSTOM_STATUS:
            custom_status = str(request.data.get("custom_status", "") or "").strip()
            if not custom_status:
                return Response({"custom_status": "Enter a status when selecting Other."}, status=400)
            if len(custom_status) > MAX_CUSTOM_STATUS_LENGTH:
                return Response({"custom_status": f"Status must be {MAX_CUSTOM_STATUS_LENGTH} characters or fewer."}, status=400)
            status = custom_status
        elif requested_status in DELIVERY_BOY_ALLOWED_STATUSES:
            status = requested_status
        else:
            return Response({"detail": "Choose delivered, picked_up, cancelled, failed, or other.", "allowed_statuses": sorted(DELIVERY_BOY_ALLOWED_STATUSES | {DELIVERY_BOY_CUSTOM_STATUS})}, status=400)
        if len(description) > 1800:
            return Response({"description": "Description must be 1800 characters or fewer."}, status=400)
        with transaction.atomic():
            order = _assigned_order(order_id, request.user.pk, lock=True)
            if order is None:
                return Response({"detail": "Assigned order not found."}, status=404)
            previous = str(order.get("status") or "").strip().lower()
            if previous in {"delivered", "cancelled"}:
                return Response({"detail": "This order is already terminal and cannot be updated."}, status=409)
            message = f"Delivery Boy #{request.user.pk}"
            if description:
                message += f": {description}"
            with connection.cursor() as cursor:
                cursor.execute("UPDATE zg_checkout SET status = %s WHERE order_id = %s AND delivery_boy_id = %s", [status, order_id, request.user.pk])
                if cursor.rowcount != 1:
                    return Response({"detail": "Assigned order not found."}, status=404)
                cursor.execute("INSERT INTO zg_order_status_history (order_id, status, changed_at, message) VALUES (%s, %s, NOW(), %s)", [order_id, status, message])
        updated = _assigned_order(order_id, request.user.pk)
        return Response(_order_payload(updated))
