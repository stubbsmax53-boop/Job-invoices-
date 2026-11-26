"""Admin pages and JSON API for the job board, logistics, and inbox.

Routes are registered from app.py. Token checks use the same intake secret.
Nothing in this module sends email or SMS.
"""

from __future__ import annotations

from datetime import timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

import jobs
from parsing import extract_address


def register_board(app) -> None:
    import app as app_module

    def _db():
        return app_module.get_db()

    def _payload() -> dict:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            return data if isinstance(data, dict) else {}
        if request.form:
            return request.form.to_dict(flat=True)
        return {}

    def _time_error(payload: dict):
        message = jobs.reject_time_tracking(payload)
        if not message:
            return None
        return jsonify({"ok": False, "error": message}), 400

    def _field_error(errors: dict[str, str]):
        first = next(iter(errors.values()))
        return jsonify({"ok": False, "error": first, "fields": errors}), 400

    def _safe_next(target: str | None, fallback: str) -> str:
        if target and target.startswith("/") and not target.startswith("//"):
            return target
        return fallback

    def _int_arg(value):
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        if not text or not text.lstrip("-").isdigit():
            return None
        return int(text)

    @app.route("/admin/home")
    @app_module.admin_required
    def dashboard():
        db = _db()
        start, end = jobs.week_bounds()
        today = jobs.today_local()
        horizon = (today + timedelta(days=7)).isoformat()
        unpaid = jobs.unpaid_invoices(db)
        return render_template(
            "dashboard.html",
            counts=jobs.status_counts(db),
            statuses=jobs.JOB_STATUSES,
            week_jobs=jobs.list_jobs(db, week=True),
            week_start=start,
            week_end=end,
            deliveries=jobs.deliveries_between(db, today.isoformat(), horizon),
            unpaid=unpaid,
            unpaid_total=jobs.money_sum([row["amount"] for row in unpaid]),
            unreviewed=jobs.unreviewed_count(db),
        )

    @app.route("/admin/jobs")
    @app_module.admin_required
    def jobs_board():
        db = _db()
        status = (request.args.get("status") or "").strip().lower()
        if status not in {key for key, _label in jobs.JOB_STATUSES}:
            status = ""
        customer = (request.args.get("customer") or "").strip()
        week = (request.args.get("week") or "").strip().lower() in {"1", "true", "yes", "this"}
        rows = jobs.list_jobs(
            db,
            status=status or None,
            customer=customer or None,
            week=week,
        )
        return render_template(
            "jobs.html",
            job_rows=rows,
            statuses=jobs.JOB_STATUSES,
            status=status,
            customer=customer,
            week=week,
            customers=jobs.list_customers(db),
            week_start=jobs.week_bounds()[0],
            week_end=jobs.week_bounds()[1],
        )

    @app.route("/admin/jobs/new", methods=["GET", "POST"])
    @app_module.admin_required
    def job_new():
        db = _db()
        errors: dict[str, str] = {}
        values = jobs.blank_job_form()
        match = None
        if request.method == "POST":
            blocked = _time_error(_payload())
            if blocked:
                errors["form"] = blocked[0].get_json()["error"]
            values = {key: request.form.get(key, "") for key in jobs.JOB_FIELDS}
            clean, field_errors = jobs.validate_job(values)
            errors.update(field_errors)
            if not errors:
                matched = jobs.find_job_by_address(
                    db, jobs.joined_address(clean["street_address"], clean["town"])
                )
                if matched is not None and request.form.get("force_new") != "1":
                    errors["street_address"] = (
                        "A job at this street is already on the board. "
                        "Open that job, or save this as a separate job."
                    )
                    match = matched
                else:
                    job_id = jobs.create_job(db, clean)
                    flash("Job saved.", "ok")
                    return redirect(url_for("job_detail", job_id=job_id))
        return render_template(
            "job_form.html",
            errors=errors,
            values=values,
            match=match,
            customers=jobs.customer_choices(db),
            roster=jobs.list_crew(db),
            statuses=jobs.JOB_STATUSES,
        )

    @app.route("/admin/jobs/<int:job_id>", methods=["GET", "POST"])
    @app_module.admin_required
    def job_detail(job_id: int):
        db = _db()
        job = jobs.get_job(db, job_id)
        if job is None:
            abort(404)
        errors: dict[str, str] = {}
        values = jobs.job_form_values(job)
        if request.method == "POST":
            blocked = _time_error(_payload())
            if blocked:
                errors["form"] = blocked[0].get_json()["error"]
            values = {key: request.form.get(key, "") for key in jobs.JOB_FIELDS}
            clean, field_errors = jobs.validate_job(values)
            errors.update(field_errors)
            if not errors:
                jobs.update_job(db, job_id, clean)
                flash("Job saved.", "ok")
                return redirect(url_for("job_detail", job_id=job_id))
            job = jobs.get_job(db, job_id)
        return render_template(
            "job.html",
            job=job,
            values=values,
            errors=errors,
            statuses=jobs.JOB_STATUSES,
            order_statuses=jobs.ORDER_STATUSES,
            need_kinds=jobs.NEED_KINDS,
            need_statuses=jobs.NEED_STATUSES,
            customers=jobs.customer_choices(db),
            roster=jobs.list_crew(db),
            orders=jobs.list_orders(db, job_id=job_id),
            needs=jobs.list_needs(db, job_id),
            invoices=jobs.list_invoices_for_job(db, job_id),
            messages=jobs.list_messages_for_job(db, job_id),
            activity=jobs.list_activity(db, job_id=job_id, limit=30),
            all_invoices=_db().execute(
                "SELECT id, invoice_number, address, job_id, status FROM invoices ORDER BY id DESC"
            ).fetchall(),
        )

    @app.route("/admin/jobs/<int:job_id>/status", methods=["POST"])
    @app_module.admin_required
    def job_status(job_id: int):
        status = (request.form.get("status") or "").strip().lower()
        if jobs.set_job_status(_db(), job_id, status) is None:
            if jobs.get_job(_db(), job_id) is None:
                abort(404)
            flash("Choose a status from Lead through Paid.", "error")
        else:
            flash("Status saved.", "ok")
        return redirect(_safe_next(request.form.get("next"), url_for("job_detail", job_id=job_id)))

    @app.route("/admin/jobs/<int:job_id>/orders", methods=["POST"])
    @app_module.admin_required
    def job_add_order(job_id: int):
        db = _db()
        if jobs.get_job(db, job_id) is None:
            abort(404)
        clean, errors = jobs.validate_order(_payload())
        if errors:
            flash(next(iter(errors.values())), "error")
            return redirect(url_for("job_detail", job_id=job_id))
        jobs.create_order(db, job_id, clean)
        flash("Material order saved.", "ok")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/admin/orders/<int:order_id>", methods=["POST"])
    @app_module.admin_required
    def order_update(order_id: int):
        db = _db()
        current = jobs.get_order(db, order_id)
        if current is None:
            abort(404)
        if request.form.get("action") == "delete":
            jobs.delete_order(db, order_id)
            flash("Material order removed.", "ok")
            return redirect(url_for("job_detail", job_id=current["job_id"]))
        clean, errors = jobs.validate_order(_payload())
        if errors:
            flash(next(iter(errors.values())), "error")
        else:
            jobs.update_order(db, order_id, clean)
            flash("Material order saved.", "ok")
        return redirect(url_for("job_detail", job_id=current["job_id"]))

    @app.route("/admin/jobs/<int:job_id>/needs", methods=["POST"])
    @app_module.admin_required
    def job_add_need(job_id: int):
        db = _db()
        if jobs.get_job(db, job_id) is None:
            abort(404)
        clean, errors = jobs.validate_need(_payload())
        if errors:
            flash(next(iter(errors.values())), "error")
            return redirect(url_for("job_detail", job_id=job_id))
        jobs.create_need(db, job_id, clean)
        flash("Saved.", "ok")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/admin/needs/<int:need_id>", methods=["POST"])
    @app_module.admin_required
    def need_update(need_id: int):
        db = _db()
        current = jobs.get_need(db, need_id)
        if current is None:
            abort(404)
        if request.form.get("action") == "delete":
            jobs.delete_need(db, need_id)
            flash("Removed.", "ok")
            return redirect(url_for("job_detail", job_id=current["job_id"]))
        clean, errors = jobs.validate_need(_payload())
        if errors:
            flash(next(iter(errors.values())), "error")
        else:
            jobs.update_need(db, need_id, clean)
            flash("Saved.", "ok")
        return redirect(url_for("job_detail", job_id=current["job_id"]))

    @app.route("/admin/jobs/<int:job_id>/invoice", methods=["POST"])
    @app_module.admin_required
    def job_create_invoice(job_id: int):
        db = _db()
        job = jobs.get_job(db, job_id)
        if job is None:
            abort(404)
        description = job["job_name"]
        if job["notes"]:
            description = f"{job['job_name']}\n\n{job['notes']}"
        invoice_id = app_module.create_invoice(
            db,
            {
                "address": jobs.joined_address(job["street_address"], job["town"]),
                "description": description,
                "tech_name": (job["crew"] or app.config["BUSINESS_NAME"]),
                "job_id": int(job["id"]),
            },
        )
        if job["amount"]:
            db.execute(
                "UPDATE invoices SET amount = ?, updated_at = ? WHERE id = ?",
                (job["amount"], app_module.now_iso(), invoice_id),
            )
            db.commit()
        flash("Invoice opened for this job.", "ok")
        return redirect(url_for("invoice_page", invoice_id=invoice_id))

    @app.route("/admin/jobs/<int:job_id>/attach-invoice", methods=["POST"])
    @app_module.admin_required
    def job_attach_invoice(job_id: int):
        db = _db()
        if jobs.get_job(db, job_id) is None:
            abort(404)
        invoice_id = _int_arg(request.form.get("invoice_id"))
        if invoice_id is None:
            flash("Choose an invoice.", "error")
            return redirect(url_for("job_detail", job_id=job_id))
        invoice = db.execute("SELECT id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if invoice is None:
            flash("That invoice is not in the list.", "error")
            return redirect(url_for("job_detail", job_id=job_id))
        db.execute(
            "UPDATE invoices SET job_id = ?, updated_at = ? WHERE id = ?",
            (job_id, app_module.now_iso(), invoice_id),
        )
        db.execute(
            """
            UPDATE messages SET job_id = ?
            WHERE invoice_id = ? AND job_id IS NULL
            """,
            (job_id, invoice_id),
        )
        db.commit()
        flash("Invoice linked.", "ok")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/admin/jobs/<int:job_id>/crew", methods=["POST"])
    @app_module.admin_required
    def job_add_crew(job_id: int):
        if jobs.get_job(_db(), job_id) is None:
            abort(404)
        if not jobs.append_crew(_db(), job_id, request.form.get("name") or ""):
            flash("Enter a name.", "error")
        else:
            flash("Crew updated.", "ok")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/admin/crew", methods=["POST"])
    @app_module.admin_required
    def crew_add():
        error = jobs.add_crew_member(_db(), request.form.get("name") or "")
        if error:
            flash(error, "error")
        else:
            flash("Name added to the crew list.", "ok")
        return redirect(_safe_next(request.form.get("next"), url_for("jobs_board")))

    @app.route("/admin/crew/<int:member_id>/delete", methods=["POST"])
    @app_module.admin_required
    def crew_delete(member_id: int):
        jobs.remove_crew_member(_db(), member_id)
        flash("Name removed from the crew list.", "ok")
        return redirect(_safe_next(request.form.get("next"), url_for("jobs_board")))

    @app.route("/admin/logistics")
    @app_module.admin_required
    def logistics_page():
        return render_template("logistics.html", items=jobs.logistics_items(_db()))

    @app.route("/admin/activity")
    @app_module.admin_required
    def activity_page():
        show = (request.args.get("show") or "unreviewed").strip().lower()
        if show not in {"unreviewed", "reviewed", "all"}:
            show = "unreviewed"
        reviewed = {"unreviewed": False, "reviewed": True, "all": None}[show]
        db = _db()
        rows = jobs.list_activity(db, reviewed=reviewed, limit=200)
        return render_template(
            "activity.html",
            rows=rows,
            show=show,
            unreviewed=jobs.unreviewed_count(db),
        )

    @app.route("/admin/activity/<int:activity_id>/review", methods=["POST"])
    @app_module.admin_required
    def activity_review(activity_id: int):
        reviewed = (request.form.get("reviewed") or "").strip() in {"1", "true", "yes", "on"}
        if not jobs.set_reviewed(_db(), activity_id, reviewed):
            abort(404)
        flash("Marked reviewed." if reviewed else "Marked unreviewed.", "ok")
        show = (request.form.get("show") or "unreviewed").strip().lower()
        if show not in {"unreviewed", "reviewed", "all"}:
            show = "unreviewed"
        return redirect(url_for("activity_page", show=show))

    @app.route("/admin/activity/mark-reviewed", methods=["POST"])
    @app_module.admin_required
    def activity_mark_all():
        count = jobs.mark_all_reviewed(_db())
        if count:
            flash(f"Marked {count} reviewed.", "ok")
        else:
            flash("Inbox is already reviewed.", "ok")
        return redirect(url_for("activity_page", show="unreviewed"))

    @app.route("/invoice/<int:invoice_id>/job", methods=["POST"])
    @app_module.admin_required
    def link_invoice_job(invoice_id: int):
        app_module.invoice_or_404(invoice_id)
        raw = (request.form.get("job_id") or "").strip()
        job_id = None
        if raw:
            if _int_arg(raw) is None or jobs.get_job(_db(), int(raw)) is None:
                flash("That job is not on the board.", "error")
                return redirect(url_for("invoice_page", invoice_id=invoice_id))
            job_id = int(raw)
        db = _db()
        db.execute(
            "UPDATE invoices SET job_id = ?, updated_at = ? WHERE id = ?",
            (job_id, app_module.now_iso(), invoice_id),
        )
        if job_id:
            db.execute(
                """
                UPDATE messages SET job_id = ?
                WHERE invoice_id = ? AND job_id IS NULL
                """,
                (job_id, invoice_id),
            )
        db.commit()
        flash("Job link saved.", "ok")
        return redirect(url_for("invoice_page", invoice_id=invoice_id))

    @app.route("/admin/messages/<int:message_id>/job", methods=["POST"])
    @app_module.admin_required
    def link_message_job(message_id: int):
        db = _db()
        message = db.execute("SELECT id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            abort(404)
        raw = (request.form.get("job_id") or "").strip()
        job_id = None
        if raw:
            if _int_arg(raw) is None or jobs.get_job(db, int(raw)) is None:
                flash("That job is not on the board.", "error")
                return redirect(url_for("message_detail", message_id=message_id))
            job_id = int(raw)
        db.execute("UPDATE messages SET job_id = ? WHERE id = ?", (job_id, message_id))
        db.execute(
            "UPDATE activity SET job_id = ? WHERE message_id = ?",
            (job_id, message_id),
        )
        db.commit()
        flash("Job link saved.", "ok")
        return redirect(url_for("message_detail", message_id=message_id))

    @app.route("/api/jobs", methods=["GET", "POST"])
    def api_jobs():
        denied = app_module.api_denied()
        if denied:
            return denied
        db = _db()
        if request.method == "GET":
            status = (request.args.get("status") or "").strip().lower()
            if status and status not in {key for key, _label in jobs.JOB_STATUSES}:
                return jsonify({"ok": False, "error": "Unknown status."}), 400
            customer = (request.args.get("customer") or "").strip()
            week = (request.args.get("week") or "").strip().lower() in {"1", "true", "yes"}
            rows = jobs.list_jobs(
                db,
                status=status or None,
                customer=customer or None,
                week=week,
            )
            return jsonify(
                {"ok": True, "count": len(rows), "jobs": [jobs.job_to_dict(row) for row in rows]}
            )
        payload = _payload()
        blocked = _time_error(payload)
        if blocked:
            return blocked
        clean, errors = jobs.validate_job(payload)
        if errors:
            return _field_error(errors)
        job_id = jobs.create_job(db, clean)
        row = jobs.get_job(db, job_id)
        activity = jobs.record_activity(
            db,
            source="assistant",
            sender=(str(payload.get("sender") or "").strip()[:500] or "assistant"),
            summary=jobs.describe_new_job(row),
            address=jobs.joined_address(row["street_address"], row["town"]),
            job_id=job_id,
        )
        return jsonify({"ok": True, "job": jobs.job_to_dict(row), "activity_id": activity["id"]}), 201

    @app.route("/api/jobs/<int:job_id>", methods=["GET", "PATCH", "PUT"])
    def api_job(job_id: int):
        denied = app_module.api_denied()
        if denied:
            return denied
        db = _db()
        row = jobs.get_job(db, job_id)
        if row is None:
            return jsonify({"ok": False, "error": "Unknown job."}), 404
        if request.method == "GET":
            return jsonify({"ok": True, "job": jobs.job_detail_dict(db, row)})
        payload = _payload()
        blocked = _time_error(payload)
        if blocked:
            return blocked
        if not any(key in payload for key in jobs.JOB_FIELDS):
            return jsonify({"ok": False, "error": "Send at least one field to update."}), 400
        merged = jobs.job_form_values(row)
        for key in jobs.JOB_FIELDS:
            if key in payload:
                merged[key] = "" if payload[key] is None else payload[key]
        before = row
        clean, errors = jobs.validate_job(merged)
        if errors:
            return _field_error(errors)
        jobs.update_job(db, job_id, clean)
        updated = jobs.get_job(db, job_id)
        activity = jobs.record_activity(
            db,
            source="assistant",
            sender=(str(payload.get("sender") or "").strip()[:500] or "assistant"),
            summary=jobs.describe_job_change(before, clean),
            address=jobs.joined_address(updated["street_address"], updated["town"]),
            job_id=job_id,
        )
        return jsonify(
            {"ok": True, "job": jobs.job_to_dict(updated), "activity_id": activity["id"]}
        )

    @app.route("/api/material-orders", methods=["GET", "POST"])
    def api_orders():
        denied = app_module.api_denied()
        if denied:
            return denied
        db = _db()
        if request.method == "GET":
            status = (request.args.get("status") or "").strip().lower()
            if status and status not in {key for key, _label in jobs.ORDER_STATUSES}:
                return jsonify({"ok": False, "error": "Unknown order status."}), 400
            job_id = None
            if request.args.get("job_id"):
                job_id = _int_arg(request.args.get("job_id"))
                if job_id is None:
                    return jsonify({"ok": False, "error": "job_id must be a number."}), 400
            open_only = (request.args.get("open") or "").strip().lower() in {"1", "true", "yes"}
            rows = jobs.list_orders(db, job_id=job_id, status=status or None, open_only=open_only)
            return jsonify({"ok": True, "orders": [jobs.order_to_dict(row) for row in rows]})
        payload = _payload()
        blocked = _time_error(payload)
        if blocked:
            return blocked
        job_id = _int_arg(payload.get("job_id"))
        if job_id is None or jobs.get_job(db, job_id) is None:
            return jsonify({"ok": False, "error": "job_id must be a job on the board."}), 400
        clean, errors = jobs.validate_order(payload)
        if errors:
            return _field_error(errors)
        order_id = jobs.create_order(db, job_id, clean)
        order = jobs.get_order(db, order_id)
        job = jobs.get_job(db, job_id)
        activity = jobs.record_activity(
            db,
            source="assistant",
            sender=(str(payload.get("sender") or "").strip()[:500] or "assistant"),
            summary=jobs.describe_order(job, clean),
            address=jobs.joined_address(job["street_address"], job["town"]),
            job_id=job_id,
        )
        return jsonify(
            {"ok": True, "order": jobs.order_to_dict(order), "activity_id": activity["id"]}
        ), 201

    @app.route("/api/material-orders/<int:order_id>", methods=["GET", "PATCH", "PUT"])
    def api_order(order_id: int):
        denied = app_module.api_denied()
        if denied:
            return denied
        db = _db()
        current = jobs.get_order(db, order_id)
        if current is None:
            return jsonify({"ok": False, "error": "Unknown material order."}), 404
        if request.method == "GET":
            return jsonify({"ok": True, "order": jobs.order_to_dict(current)})
        payload = _payload()
        blocked = _time_error(payload)
        if blocked:
            return blocked
        if not any(key in payload for key in jobs.ORDER_FIELDS):
            return jsonify({"ok": False, "error": "Send at least one field to update."}), 400
        merged = jobs.order_form_values(current)
        for key in jobs.ORDER_FIELDS:
            if key in payload:
                merged[key] = "" if payload[key] is None else payload[key]
        clean, errors = jobs.validate_order(merged)
        if errors:
            return _field_error(errors)
        jobs.update_order(db, order_id, clean)
        updated = jobs.get_order(db, order_id)
        job = jobs.get_job(db, int(current["job_id"]))
        activity = jobs.record_activity(
            db,
            source="assistant",
            sender=(str(payload.get("sender") or "").strip()[:500] or "assistant"),
            summary=jobs.describe_order(job, clean, previous=current),
            address=jobs.joined_address(job["street_address"], job["town"]),
            job_id=int(job["id"]),
        )
        return jsonify(
            {"ok": True, "order": jobs.order_to_dict(updated), "activity_id": activity["id"]}
        )

    @app.route("/api/activity", methods=["GET", "POST"])
    def api_activity():
        denied = app_module.api_denied()
        if denied:
            return denied
        db = _db()
        if request.method == "GET":
            flag = (request.args.get("reviewed") or "").strip().lower()
            reviewed = None
            if flag in {"0", "false", "unreviewed", "no"}:
                reviewed = False
            elif flag in {"1", "true", "reviewed", "yes"}:
                reviewed = True
            elif flag:
                return jsonify({"ok": False, "error": "reviewed is 0 or 1."}), 400
            job_id = None
            if request.args.get("job_id"):
                job_id = _int_arg(request.args.get("job_id"))
                if job_id is None:
                    return jsonify({"ok": False, "error": "job_id must be a number."}), 400
            limit = _int_arg(request.args.get("limit") or 100) or 100
            rows = jobs.list_activity(db, reviewed=reviewed, job_id=job_id, limit=limit)
            return jsonify(
                {
                    "ok": True,
                    "unreviewed": jobs.unreviewed_count(db),
                    "activity": [jobs.activity_to_dict(row) for row in rows],
                }
            )
        payload = _payload()
        blocked = _time_error(payload)
        if blocked:
            return blocked
        clean, errors = jobs.validate_activity(payload)
        if errors:
            return _field_error(errors)
        if clean["job_id"] is not None and jobs.get_job(db, int(clean["job_id"])) is None:
            return jsonify({"ok": False, "error": "Unknown job."}), 400
        address = clean["address"]
        if not address and clean["job_id"] is None:
            address = extract_address(f"{clean['summary']}\n{clean['body']}")
        try:
            stored = jobs.record_activity(
                db,
                source=clean["source"],
                sender=clean["sender"],
                summary=clean["summary"],
                body=clean["body"],
                address=address,
                job_id=clean["job_id"],
                external_id=clean["external_id"],
                received_at=clean["received_at"],
            )
        except LookupError:
            return jsonify({"ok": False, "error": "Unknown job."}), 400
        status = 200 if stored.get("duplicate") else 201
        return jsonify({"ok": True, "activity": stored}), status
