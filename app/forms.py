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
"""WTForms definitions (also provide CSRF protection on every POST)."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    DateField,
    HiddenField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional, URL

from .models import (
    ASSET_KINDS,
    ATTACHMENT_KINDS,
    CONTACT_TYPES,
    STATUSES,
    SUBMISSION_STATUSES,
    WORK_MODES,
)


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=80)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")


class JobForm(FlaskForm):
    company = StringField("Company", validators=[DataRequired(), Length(max=160)])
    title = StringField("Job title", validators=[DataRequired(), Length(max=160)])
    location = StringField("Location", validators=[Optional(), Length(max=160)])
    work_mode = SelectField("Work mode", choices=[(m, m) for m in WORK_MODES])
    status = SelectField("Status", choices=[(s, s) for s in STATUSES])
    source = StringField("Source", validators=[Optional(), Length(max=80)],
                         description="Indeed, LinkedIn, referral, company site, etc.")
    url = StringField("Posting URL", validators=[Optional(), URL(), Length(max=500)])
    salary = StringField("Salary / range", validators=[Optional(), Length(max=80)])
    date_applied = DateField("Date applied", validators=[Optional()])
    follow_up_date = DateField("Follow-up date", validators=[Optional()])
    contact_name = StringField("Contact name", validators=[Optional(), Length(max=120)])
    contact_email = StringField("Contact email", validators=[Optional(), Email(), Length(max=160)])
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save")


class InterviewForm(FlaskForm):
    interview_date = DateField("Interview date", validators=[Optional()])
    round_type = StringField("Round", validators=[Optional(), Length(max=80)],
                             description="Phone screen, technical, panel, final, etc.")
    interview_format = SelectField(
        "Format",
        choices=[("", "—"), ("Phone", "Phone"), ("Video", "Video"), ("On-site", "On-site")],
        validators=[Optional()],
    )
    interviewer = StringField("Interviewer(s)", validators=[Optional(), Length(max=160)])
    self_rating = SelectField(
        "How it felt (1-5)",
        choices=[("", "—"), ("1", "1 - poor"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5 - great")],
        validators=[Optional()],
    )
    questions_asked = TextAreaField("Questions they asked", validators=[Optional()],
                                    description="Capture each question while it is fresh.")
    went_well = TextAreaField("What went well", validators=[Optional()])
    to_improve = TextAreaField("What to improve", validators=[Optional()])
    notes = TextAreaField("Other notes", validators=[Optional()])
    submit = SubmitField("Save debrief")


class AttachmentForm(FlaskForm):
    kind = SelectField("Type", choices=[(k, k) for k in ATTACHMENT_KINDS])
    file = FileField(
        "File",
        validators=[
            FileRequired(),
            FileAllowed(["pdf", "doc", "docx", "txt", "rtf", "odt"], "Documents only."),
        ],
    )
    submit = SubmitField("Upload")


class AIImportForm(FlaskForm):
    payload = TextAreaField("Paste Claude's JSON analysis", validators=[Optional()])
    file = FileField("...or upload a .json file",
                     validators=[Optional(), FileAllowed(["json", "txt"], "JSON only.")])
    submit = SubmitField("Import analysis")


class KitForm(FlaskForm):
    """Standalone application-kit generator for a job not yet in Job Squire."""
    job_title = StringField("Job title", validators=[DataRequired(), Length(max=160)])
    company = StringField("Company", validators=[DataRequired(), Length(max=160)])
    location = StringField("Location", validators=[Optional(), Length(max=160)])
    url = StringField("Posting URL", validators=[Optional(), URL(), Length(max=500)])
    job_description = TextAreaField("Paste the full job description",
                                    validators=[DataRequired()])
    save_job = SelectField("Also add this to Job Squire?",
                           choices=[("no", "No, just the kit"), ("yes", "Yes, save as a job too")])
    submit = SubmitField("Generate application kit")


class ContactForm(FlaskForm):
    """A recruiter / staffing-agency / networking contact."""
    name = StringField("Name", validators=[DataRequired(), Length(max=160)])
    contact_type = SelectField("Type", choices=[(t, t) for t in CONTACT_TYPES])
    title = StringField("Their title", validators=[Optional(), Length(max=160)],
                        description="e.g. Senior Recruiter, Branch Manager")
    agency = StringField("Agency / company", validators=[Optional(), Length(max=160)],
                         description="Robert Half, Manpower, Randstad, the hiring company, etc.")
    email = StringField("Email", validators=[Optional(), Email(), Length(max=160)])
    phone = StringField("Phone", validators=[Optional(), Length(max=60)])
    linkedin_url = StringField("LinkedIn URL", validators=[Optional(), URL(), Length(max=500)])
    last_contacted = DateField("Last contacted", validators=[Optional()])
    follow_up_date = DateField("Follow-up date", validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional()],
                          description="What they cover, how you met, what they said.")
    submit = SubmitField("Save contact")


class SubmissionForm(FlaskForm):
    """A submission: a recruiter/agency put User forward for a specific role."""
    contact_id = SelectField("Submitted by", validators=[Optional()], coerce=str)
    company = StringField("Company submitted to", validators=[Optional(), Length(max=160)])
    role_title = StringField("Role / title", validators=[Optional(), Length(max=160)])
    job_id = SelectField("Link to a tracked job (optional)", validators=[Optional()], coerce=str)
    status = SelectField("Status", choices=[(s, s) for s in SUBMISSION_STATUSES])
    submitted_date = DateField("Date submitted", validators=[Optional()])
    follow_up_date = DateField("Follow-up date", validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save submission")


class CandidateAssetForm(FlaskForm):
    """Upload a master candidate document (resume, rec letter, cert, etc.)."""
    kind = SelectField("Document type", choices=[(k, k) for k in ASSET_KINDS])
    label = StringField(
        "Label",
        validators=[Optional(), Length(max=255)],
        description='Short name, e.g. "ATS Resume v3" or "Amy Draper — Letter of Rec"',
    )
    notes = TextAreaField(
        "Notes for Claude",
        validators=[Optional()],
        description="Context Claude should know when using this file — e.g. which roles it was written for.",
    )
    file = FileField(
        "File",
        validators=[
            FileRequired(),
            FileAllowed(
                ["pdf", "doc", "docx", "txt", "rtf", "odt", "md", "png", "jpg", "jpeg"],
                "Documents and images only.",
            ),
        ],
    )
    submit = SubmitField("Upload")


class CandidateAssetEditForm(FlaskForm):
    """Edit label and notes on an existing candidate asset (no re-upload)."""
    kind = SelectField("Document type", choices=[(k, k) for k in ASSET_KINDS])
    label = StringField("Label", validators=[Optional(), Length(max=255)])
    notes = TextAreaField("Notes for Claude", validators=[Optional()])
    submit = SubmitField("Save")


class ConfirmForm(FlaskForm):
    """Bare form used to CSRF-protect delete/confirm buttons."""
    submit = SubmitField("Confirm")
