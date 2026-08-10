"""Legal documents blueprint — admin CRUD + worker signing API."""
from __future__ import annotations
import hashlib

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, abort
from flask_login import current_user
from flask_jwt_extended import jwt_required
from sqlalchemy.orm import joinedload

from .. import app, db
from ..models import Cleaner, LegalDocument, DocumentSignature
from ..utils import admin_required, _verify_worker_id

bp = Blueprint('documents', __name__)


# ── Admin ────────────────────────────────────────────────────────────────────

@bp.route('/admin/documents')
@admin_required
def admin_documents():
    docs = LegalDocument.query.order_by(LegalDocument.created_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    return render_template('admin_documents.html', documents=docs, workers=workers)


@bp.route('/admin/documents/create', methods=['POST'])
@admin_required
def create_document():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    doc_type = request.form.get('doc_type', '').strip()
    if not title or not content:
        flash('El título y el contenido son obligatorios.', 'error')
        return redirect(url_for('documents.admin_documents'))
    doc = LegalDocument(
        title=title, content=content, doc_type=doc_type or None,
        created_by=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash('Documento creado correctamente.', 'success')
    return redirect(url_for('documents.admin_documents'))


@bp.route('/admin/documents/<int:doc_id>/edit', methods=['POST'])
@admin_required
def edit_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    if doc.signatures:
        flash('No se puede editar un documento que ya tiene firmas.', 'error')
        return redirect(url_for('documents.admin_documents'))
    doc.title = request.form.get('title', '').strip() or doc.title
    doc.content = request.form.get('content', '').strip() or doc.content
    doc.doc_type = request.form.get('doc_type', '').strip() or doc.doc_type
    db.session.commit()
    flash('Documento actualizado.', 'success')
    return redirect(url_for('documents.admin_documents'))


@bp.route('/admin/documents/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    if doc.signatures:
        flash('No se puede eliminar un documento que ya tiene firmas.', 'error')
        return redirect(url_for('documents.admin_documents'))
    db.session.delete(doc)
    db.session.commit()
    flash('Documento eliminado.', 'success')
    return redirect(url_for('documents.admin_documents'))


@bp.route('/admin/documents/<int:doc_id>/toggle', methods=['POST'])
@admin_required
def toggle_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    doc.active = not doc.active
    db.session.commit()
    flash(f'Documento {"activado" if doc.active else "desactivado"}.', 'success')
    return redirect(url_for('documents.admin_documents'))


@bp.route('/admin/documents/<int:doc_id>/signatures')
@admin_required
def document_signatures(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        abort(404)
    sigs = DocumentSignature.query.filter_by(document_id=doc_id)\
        .options(joinedload(DocumentSignature.cleaner))\
        .order_by(DocumentSignature.signed_at.desc()).all()
    workers = Cleaner.query.filter_by(active=True).order_by(Cleaner.name).all()
    signed_ids = {s.cleaner_id for s in sigs}
    unsigned = [w for w in workers if w.id not in signed_ids]
    return render_template('admin_document_signatures.html',
                           document=doc, signatures=sigs, unsigned=unsigned)


# ── Worker API ───────────────────────────────────────────────────────────────

@bp.route('/api/worker/pending-documents')
@jwt_required()
def pending_documents():
    worker_id = request.args.get('worker_id', type=int)
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    signed = db.session.query(DocumentSignature.document_id)\
        .filter_by(cleaner_id=worker_id).subquery()
    docs = LegalDocument.query.filter_by(active=True)\
        .filter(~LegalDocument.id.in_(signed))\
        .order_by(LegalDocument.created_at).all()
    return jsonify([{
        'id': d.id, 'title': d.title, 'doc_type': d.doc_type or '',
    } for d in docs])


@bp.route('/api/worker/document/<int:doc_id>')
@jwt_required()
def get_document(doc_id: int):
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        return jsonify({'error': 'Documento no encontrado'}), 404
    return jsonify({
        'id': doc.id, 'title': doc.title, 'content': doc.content,
        'doc_type': doc.doc_type or '',
    })


@bp.route('/api/worker/document/<int:doc_id>/sign', methods=['POST'])
@jwt_required()
def sign_document(doc_id: int):
    from .nfc import _save_base64_photo
    data = request.json or {}
    worker_id = data.get('worker_id')
    if worker_id and not _verify_worker_id(worker_id):
        return jsonify({'error': 'No autorizado'}), 403
    photo = data.get('photo')
    if not worker_id:
        return jsonify({'error': 'worker_id requerido'}), 400
    doc = db.session.get(LegalDocument, doc_id)
    if not doc:
        return jsonify({'error': 'Documento no encontrado'}), 404
    existing = DocumentSignature.query.filter_by(
        document_id=doc_id, cleaner_id=worker_id).first()
    if existing:
        return jsonify({'error': 'Ya has firmado este documento'}), 400
    selfie_path = None
    if photo:
        try:
            selfie_path = _save_base64_photo(photo, 'signing_selfies', int(worker_id))
        except ValueError:
            pass
    content_hash = hashlib.sha256(doc.content.encode()).hexdigest()
    sig = DocumentSignature(
        document_id=doc_id, cleaner_id=int(worker_id),
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent', '')[:500],
        selfie_path=selfie_path, content_hash=content_hash,
    )
    db.session.add(sig)
    db.session.commit()
    return jsonify({'ok': True})
