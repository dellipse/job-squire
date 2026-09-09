# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Contacts blueprint: recruiters/agencies/hiring managers and submissions.

Split out of app/main.py (QUAL-01, Long-term audit item 17).
"""
import csv
import io
from datetime import date, datetime, timezone

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .db_utils import commit
from .extensions import db
from .forms import ConfirmForm, ContactForm, SubmissionForm
from .main import _csv_safe
from .models import CONTACT_TYPES, Contact, Job, Submission

contacts_bp = Blueprint("contacts", __name__)


def _apply_contact_form(contact, form):
    contact.name = form.name.data.strip()
    contact.contact_type = form.contact_type.data
    contact.title = (form.title.data or "").strip()
    contact.agency = (form.agency.data or "").strip()
    contact.email = (form.email.data or "").strip()
    contact.phone = (form.phone.data or "").strip()
    contact.linkedin_url = (form.linkedin_url.data or "").strip()
    contact.last_contacted = form.last_contacted.data
    contact.follow_up_date = form.follow_up_date.data
    contact.notes = form.notes.data or ""


def _populate_submission_choices(form):
    contacts = Contact.query.order_by(Contact.name.asc()).all()
    form.contact_id.choices = [("", "— none —")] + [
        (str(c.id), f"{c.name}{(' · ' + c.agency) if c.agency else ''}") for c in contacts
    ]
    jobs = Job.query.order_by(Job.company.asc(), Job.title.asc()).all()
    form.job_id.choices = [("", "— none —")] + [
        (str(j.id), f"{j.company} — {j.title}") for j in jobs
    ]


def _apply_submission_form(sub, form):
    sub.contact_id = int(form.contact_id.data) if form.contact_id.data else None
    sub.job_id = int(form.job_id.data) if form.job_id.data else None
    sub.company = (form.company.data or "").strip()
    sub.role_title = (form.role_title.data or "").strip()
    sub.status = form.status.data
    sub.submitted_date = form.submitted_date.data
    sub.follow_up_date = form.follow_up_date.data
    sub.notes = form.notes.data or ""
    # If linked to a tracked job and company/role left blank, fill from the job.
    if sub.job_id and (not sub.company or not sub.role_title):
        job = db.session.get(Job, sub.job_id)
        if job:
            sub.company = sub.company or job.company
            sub.role_title = sub.role_title or job.title


@contacts_bp.route("/contacts")
@login_required
def contacts_list():
    ctype = request.args.get("type", "").strip()
    q = request.args.get("q", "").strip()
    query = Contact.query
    if ctype and ctype in CONTACT_TYPES:
        query = query.filter(Contact.contact_type == ctype)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Contact.name.ilike(like), Contact.agency.ilike(like),
                                    Contact.email.ilike(like)))
    contacts = query.order_by(Contact.follow_up_date.is_(None), Contact.follow_up_date.asc(),
                              Contact.name.asc()).all()
    return render_template("contacts.html", contacts=contacts, contact_types=CONTACT_TYPES,
                           current_type=ctype, q=q, today=date.today())


@contacts_bp.route("/contacts/new", methods=["GET", "POST"])
@login_required
def contact_new():
    form = ContactForm()
    if form.validate_on_submit():
        contact = Contact(created_by=current_user.display_name or current_user.username)
        _apply_contact_form(contact, form)
        db.session.add(contact)
        commit()
        flash("Contact added.", "success")
        return redirect(url_for("contacts.contact_detail", contact_id=contact.id))
    return render_template("contact_form.html", form=form, mode="new")


@contacts_bp.route("/contacts/<int:contact_id>")
@login_required
def contact_detail(contact_id):
    contact = db.get_or_404(Contact, contact_id)
    return render_template("contact_detail.html", contact=contact, today=date.today(),
                           confirm_form=ConfirmForm())


@contacts_bp.route("/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
@login_required
def contact_edit(contact_id):
    contact = db.get_or_404(Contact, contact_id)
    form = ContactForm(obj=contact)
    if form.validate_on_submit():
        _apply_contact_form(contact, form)
        commit()
        flash("Contact updated.", "success")
        return redirect(url_for("contacts.contact_detail", contact_id=contact.id))
    return render_template("contact_form.html", form=form, mode="edit", contact=contact)


@contacts_bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
def contact_delete(contact_id):
    if not ConfirmForm().validate_on_submit():
        abort(400)
    contact = db.get_or_404(Contact, contact_id)
    db.session.delete(contact)
    commit()
    flash("Contact deleted.", "success")
    return redirect(url_for("contacts.contacts_list"))


@contacts_bp.route("/export/contacts.csv")
@login_required
def export_contacts_csv():
    contacts = Contact.query.order_by(Contact.name.asc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Name", "Type", "Title", "Agency", "Email", "Phone", "LinkedIn",
        "Last contacted", "Follow-up date", "Open submissions", "Notes",
    ])
    for c in contacts:
        w.writerow([
            _csv_safe(c.name), _csv_safe(c.contact_type), _csv_safe(c.title),
            _csv_safe(c.agency), _csv_safe(c.email), _csv_safe(c.phone),
            _csv_safe(c.linkedin_url),
            c.last_contacted or "", c.follow_up_date or "", len(c.open_submissions),
            _csv_safe((c.notes or "").replace("\n", " ")),
        ])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=contacts-{stamp}.csv"},
    )


@contacts_bp.route("/submissions/new", methods=["GET", "POST"])
@login_required
def submission_new():
    form = SubmissionForm()
    _populate_submission_choices(form)
    if form.validate_on_submit():
        sub = Submission(created_by=current_user.display_name or current_user.username)
        _apply_submission_form(sub, form)
        db.session.add(sub)
        commit()
        flash("Submission logged.", "success")
        if sub.contact_id:
            return redirect(url_for("contacts.contact_detail", contact_id=sub.contact_id))
        return redirect(url_for("contacts.contacts_list"))
    if request.method == "GET":
        form.contact_id.data = request.args.get("contact_id", "")
        form.job_id.data = request.args.get("job_id", "")
        form.submitted_date.data = date.today()
    return render_template("submission_form.html", form=form, mode="new")


@contacts_bp.route("/submissions/<int:sub_id>/edit", methods=["GET", "POST"])
@login_required
def submission_edit(sub_id):
    sub = db.get_or_404(Submission, sub_id)
    form = SubmissionForm(obj=sub)
    _populate_submission_choices(form)
    if request.method == "GET":
        form.contact_id.data = str(sub.contact_id) if sub.contact_id else ""
        form.job_id.data = str(sub.job_id) if sub.job_id else ""
    if form.validate_on_submit():
        _apply_submission_form(sub, form)
        commit()
        flash("Submission updated.", "success")
        if sub.contact_id:
            return redirect(url_for("contacts.contact_detail", contact_id=sub.contact_id))
        return redirect(url_for("contacts.contacts_list"))
    return render_template("submission_form.html", form=form, mode="edit", submission=sub)


@contacts_bp.route("/submissions/<int:sub_id>/delete", methods=["POST"])
@login_required
def submission_delete(sub_id):
    if not ConfirmForm().validate_on_submit():
        abort(400)
    sub = db.get_or_404(Submission, sub_id)
    contact_id = sub.contact_id
    db.session.delete(sub)
    commit()
    flash("Submission removed.", "success")
    if contact_id:
        return redirect(url_for("contacts.contact_detail", contact_id=contact_id))
    return redirect(url_for("contacts.contacts_list"))
