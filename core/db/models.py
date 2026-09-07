import uuid
from sqlalchemy import Column, String, Float, Boolean, Text, DateTime, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False)  # the person's NIP
    name = Column(String(255))  # display name (full name)
    email = Column(String(255), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    # The role KEY (see Role.key), not an FK — the historical role columns
    # elsewhere (results.uploaded_by_role, qc_status_events.actor_role, ...) store
    # the same string, so keeping it textual means they stay readable together.
    role = Column(String(50), nullable=False, default="sales_agent")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class Role(Base):
    """A role definition: what it may do (``permissions``) and whose tickets it
    sees (``data_scope``).

    Roles used to be literal strings compared against hardcoded lists all over the
    codebase; they are data now so SPQ Head / Admin can create new ones from the
    Manage Role menu. ``is_system`` marks the ten roles that shipped before this
    table existed — those cannot be deleted.
    """
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False)
    label = Column(String(100), nullable=False)
    is_system = Column(Boolean, nullable=False, default=False)
    # Which existing role was used as the template when this one was created.
    base_role = Column(String(50))
    data_scope = Column(String(30), nullable=False, default="all")
    permissions = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now())
    created_by = Column(Integer, nullable=True)


class RoleCampaign(Base):
    """Campaign a role is limited to. NO rows for a role = every campaign."""
    __tablename__ = "role_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    campaign = Column(String(100), nullable=False)


class UserCampaign(Base):
    """Campaign SATU ORANG dibatasi ke sana (tab "Assign Role" di menu Manage Role).

    TIDAK ADA baris untuk seorang user = user itu tidak dibatasi di tingkat orang;
    yang berlaku hanya batas dari role-nya (``role_campaigns``) dan — untuk cakupan
    sales — tag Dedicated di Sales Database.

    Ada baris = batas ATAS tambahan: campaign efektif user adalah IRISAN dari
    ketiganya, tidak pernah gabungan. Jadi assign di sini hanya bisa MEMPERSEMPIT,
    tidak pernah memberi akses yang tidak dipunyai role-nya (lihat
    ``api.rbac.effective_campaigns_for``).
    """
    __tablename__ = "user_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign = Column(String(100), nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    prompt_text = Column(Text, nullable=False)
    scorecard_text = Column(Text, nullable=False)
    kb_text = Column(Text, nullable=False)
    # Original filenames as uploaded (for the dashboard viewer); nullable for
    # campaigns created before this was tracked.
    prompt_filename = Column(String(255))
    scorecard_filename = Column(String(255))
    kb_filename = Column(String(255))
    # RIPLAY (Ringkasan Informasi Produk dan Layanan) — the bank's product fact
    # sheet, treated as ground truth for the product values quoted on the call.
    # ``kb_text_raw`` keeps the KB exactly as uploaded; ``kb_text`` is that text
    # with the RIPLAY extraction overlaid (see compliance/riplay.py), so the
    # overlay can always be regenerated from a pristine base.
    kb_text_raw = Column(Text)
    riplay_filename = Column(String(255))
    riplay_product_name = Column(String(255))
    riplay_similarity = Column(Float)
    riplay_extraction = Column(JSONB)
    riplay_applied = Column(JSONB)
    riplay_uploaded_at = Column(DateTime)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Result(Base):
    __tablename__ = "results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign = Column(String(100))
    source_files = Column(JSONB)
    num_calls = Column(Integer)
    transcript_path = Column(String(500))
    status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text)
    result_path = Column(String(500))
    # Who uploaded the transcript (captured at upload time). Stored as plain
    # username/role strings so API-key ("system") uploads work too.
    uploaded_by_username = Column(String(100))
    uploaded_by_role = Column(String(20))
    uploaded_at = Column(DateTime, server_default=func.now())
    # Wall-clock "Generated" timestamp parsed from the transcript PDF header (latest
    # across the ticket's PDFs). Naive/local — drives ONLY the Statistics AI-status
    # chart x-axis; NULL falls back to uploaded_at there. See compliance/pdf_parser.py.
    generated_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    processing_sec = Column(Float)


class ResultData(Base):
    __tablename__ = "result_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(UUID(as_uuid=True), ForeignKey("results.id", ondelete="CASCADE"))
    result_json = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class TmsCashline(Base):
    """CASHLINE reference data (mirrors the TMS export, ex ``data_tms_*.csv``).

    One row per submission, looked up by ``result_id`` (the customer/session ID
    derived from the earliest PDF filename prefix). Every source column is a real
    column (DB column name = the original export header, hyphens preserved via
    quoting; the Python attribute is the sanitized name). MVP mockup of the real
    TMS table; production swaps the DB connection for the real schema.

    All columns are ``Text`` (the export is untyped strings; numeric parsing is
    done downstream by the reference-data builder).
    """
    __tablename__ = "tms_cashline"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column("result_id", String(100), index=True, nullable=False)
    prospect_id = Column("prospect_id", Text)
    customer_id = Column("customer_id", Text)
    file_id = Column("file_id", Text)
    cust_name = Column("cust_name", Text)
    agent_id = Column("agent_id", Text)
    submit_time = Column("submit_time", Text)
    qc_id = Column("qc_id", Text)
    approve_time = Column("approve_time", Text)
    turli = Column("turli", Text)
    segmen = Column("segmen", Text)
    alamat_pengiriman = Column("alamat-pengiriman", Text)
    send_mail_to = Column("send-mail-to", Text)
    send_message_to = Column("send-message-to", Text)
    no_npwp_new = Column("no-npwp-new", Text)
    no_telp_rumah_new = Column("no-telp-rumah-new", Text)
    no_telp_kantor_new = Column("no-telp-kantor-new", Text)
    nik_new = Column("nik-new", Text)
    e_statement_new = Column("e-statement-new", Text)
    alamat_email_new = Column("alamat-email-new", Text)
    kirim_pesan_melalui = Column("kirim-pesan-melalui", Text)
    kantor_rt_new = Column("kantor-rt-new", Text)
    kantor_rw_new = Column("kantor-rw-new", Text)
    kantor_provinsi_new = Column("kantor-provinsi-new", Text)
    kantor_kabupatenkota_new = Column("kantor-kabupatenkota-new", Text)
    kantor_kecamatan_new = Column("kantor-kecamatan-new", Text)
    kantor_kelurahan_new = Column("kantor-kelurahan-new", Text)
    kantor_kode_pos_new = Column("kantor-kode-pos-new", Text)
    nama_perusahaan_new = Column("nama-perusahaan-new", Text)
    alamat_rumah_1_new = Column("alamat-rumah-1-new", Text)
    rumah_rt_new = Column("rumah-rt-new", Text)
    rumah_rw_new = Column("rumah-rw-new", Text)
    rumah_provinsi_new = Column("rumah-provinsi-new", Text)
    rumah_kabupatenkota_new = Column("rumah-kabupatenkota-new", Text)
    rumah_kecamatan_new = Column("rumah-kecamatan-new", Text)
    rumah_kelurahan_new = Column("rumah-kelurahan-new", Text)
    rumah_kode_pos_new = Column("rumah-kode-pos-new", Text)
    alamat_kantor_1_new = Column("alamat-kantor-1-new", Text)
    alamat_rumah_2_new = Column("alamat-rumah-2-new", Text)
    alamat_kantor_2_new = Column("alamat-kantor-2-new", Text)
    checlist_jika_ada_perubahan = Column("checlist-jika-ada-perubahan", Text)
    source_code = Column("source-code", Text)
    jenis_kartu_yang_dikehendaki = Column("jenis-kartu-yang-dikehendaki", Text)
    max_transfer = Column("max-transfer", Text)
    nominal_transfer = Column("nominal-transfer", Text)
    tenor = Column("tenor", Text)
    cicilan_per_bulan = Column("cicilan-per-bulan", Text)
    plan_code = Column("plan-code", Text)
    nama_bank = Column("nama-bank", Text)
    nomor_rekening = Column("nomor-rekening", Text)
    nama_di_rekening = Column("nama-di-rekening", Text)
    admin_fee = Column("admin-fee", Text)
    # Product fees the TMS export does not carry yet; added so they can be held as
    # data (and overridden per ticket, e.g. a promo) instead of being constants in
    # reference_data.py. Left empty, the RIPLAY TnC Product value is used instead.
    provisi = Column("provisi", Text)
    penalti_pelunasan_dipercepat = Column("penalti-pelunasan-dipercepat", Text)
    biaya_transfer = Column("biaya-transfer", Text)
    alamat_rumah_chk = Column("alamat-rumah-chk", Text)
    alamat_kantor_chk = Column("alamat-kantor-chk", Text)
    alamat_pengiriman_kartu_chk = Column("alamat-pengiriman-kartu-chk", Text)
    no_npwp_chk = Column("no-npwp-chk", Text)
    no_telp_rumah_chk = Column("no-telp-rumah-chk", Text)
    no_telp_kantor_chk = Column("no-telp-kantor-chk", Text)
    nik_chk = Column("nik-chk", Text)
    e_statement_chk = Column("e-statement-chk", Text)
    alamat_email_chk = Column("alamat-email-chk", Text)
    send_message = Column("send-message", Text)
    pekerjaan = Column("pekerjaan", Text)
    bidang_usaha = Column("bidang-usaha", Text)
    jabatan = Column("jabatan", Text)
    no_ktpkitas = Column("no-ktpkitas", Text)
    pendaftaran_credit_shield = Column("pendaftaran-credit-shield", Text)
    privy = Column("privy", Text)
    tanpa_perubahan_data_ntb_chk = Column("tanpa-perubahan-data-ntb-chk", Text)
    grab = Column("grab", Text)
    enroll = Column("enroll", Text)
    finance = Column("finance", Text)
    status = Column("status", Text)
    los = Column("los", Text)
    cld_amount = Column("cld-amount", Text)


class AscendCustp(Base):
    """CARD HOLDER reference data (mirrors the Ascend export, ex ``data_ascend_*.csv``).

    Matched against the cashline ``cust_name`` via ``cust_local_name``
    (``CUST_LOCAL_NAME``) — the lookup trims + lower-cases both sides. Every
    source column is a real column (DB column name = the original UPPERCASE export
    header, preserved via quoting). MVP mockup of the real Ascend customer table.

    All columns are ``Text`` (the export is untyped strings).
    """
    __tablename__ = "ascend_custp"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cust_nbr = Column("CUST_NBR", Text)
    cust_oper_code = Column("CUST_OPER_CODE", Text)
    cust_type = Column("CUST_TYPE", Text)
    cust_local_name = Column("CUST_LOCAL_NAME", String(255), index=True, nullable=False)
    cust_addr1 = Column("CUST_ADDR1", Text)
    cust_addr2 = Column("CUST_ADDR2", Text)
    cust_add_city = Column("CUST_ADD_CITY", Text)
    cust_add_province = Column("CUST_ADD_PROVINCE", Text)
    cust_add_zipcode = Column("CUST_ADD_ZIPCODE", Text)
    cust_phone = Column("CUST_PHONE", Text)
    cust_res_type = Column("CUST_RES_TYPE", Text)
    cust_res_period = Column("CUST_RES_PERIOD", Text)
    cust_sex = Column("CUST_SEX", Text)
    cust_marital_st = Column("CUST_MARITAL_ST", Text)
    cust_qualification = Column("CUST_QUALIFICATION", Text)
    cust_id_nbr = Column("CUST_ID_NBR", Text)
    cust_dte_birth = Column("CUST_DTE_BIRTH", Text)
    cust_mom_name = Column("CUST_MOM_NAME", Text)
    cust_work_id = Column("CUST_WORK_ID", Text)
    cust_nationality = Column("CUST_NATIONALITY", Text)
    cust_supp_rlnship = Column("CUST_SUPP_RLNSHIP", Text)
    cust_emp_name = Column("CUST_EMP_NAME", Text)
    cust_emp_addr1 = Column("CUST_EMP_ADDR1", Text)
    cust_emp_addr2 = Column("CUST_EMP_ADDR2", Text)
    cust_emp_addr3 = Column("CUST_EMP_ADDR3", Text)
    cust_emp_addr4 = Column("CUST_EMP_ADDR4", Text)
    cust_emp_city = Column("CUST_EMP_CITY", Text)
    cust_emp_zip = Column("CUST_EMP_ZIP", Text)
    cust_occ_code = Column("CUST_OCC_CODE", Text)
    cust_occ_per = Column("CUST_OCC_PER", Text)
    cust_emp_phone = Column("CUST_EMP_PHONE", Text)
    cust_ann_salary = Column("CUST_ANN_SALARY", Text)
    cust_oth_income = Column("CUST_OTH_INCOME", Text)
    cust_grlnship = Column("CUST_GRLNSHIP", Text)
    cust_glocal_name = Column("CUST_GLOCAL_NAME", Text)
    cust_geng_name = Column("CUST_GENG_NAME", Text)
    cust_gaddr1 = Column("CUST_GADDR1", Text)
    cust_gaddr2 = Column("CUST_GADDR2", Text)
    cust_gaddr3 = Column("CUST_GADDR3", Text)
    cust_gaddr4 = Column("CUST_GADDR4", Text)
    cust_gadd_city = Column("CUST_GADD_CITY", Text)
    cust_gadd_province = Column("CUST_GADD_PROVINCE", Text)
    cust_gadd_zipcode = Column("CUST_GADD_ZIPCODE", Text)
    cust_gphone = Column("CUST_GPHONE", Text)
    cust_gsex = Column("CUST_GSEX", Text)
    cust_gmarital_st = Column("CUST_GMARITAL_ST", Text)
    cust_gqualification = Column("CUST_GQUALIFICATION", Text)
    cust_gid_nbr = Column("CUST_GID_NBR", Text)
    cust_plc_birth = Column("CUST_PLC_BIRTH", Text)
    cust_gdte_birth = Column("CUST_GDTE_BIRTH", Text)
    cust_gwork_id = Column("CUST_GWORK_ID", Text)
    cust_gemp_name = Column("CUST_GEMP_NAME", Text)
    cust_gemp_addr1 = Column("CUST_GEMP_ADDR1", Text)
    cust_gemp_addr2 = Column("CUST_GEMP_ADDR2", Text)
    cust_gemp_zip = Column("CUST_GEMP_ZIP", Text)
    cust_gocc_code = Column("CUST_GOCC_CODE", Text)
    cust_gocc_per = Column("CUST_GOCC_PER", Text)
    cust_gemp_phone = Column("CUST_GEMP_PHONE", Text)
    cust_gann_salary = Column("CUST_GANN_SALARY", Text)
    cust_maddr1 = Column("CUST_MADDR1", Text)
    cust_maddr2 = Column("CUST_MADDR2", Text)
    cust_madd_city = Column("CUST_MADD_CITY", Text)
    cust_madd_province = Column("CUST_MADD_PROVINCE", Text)
    cust_madd_zipcode = Column("CUST_MADD_ZIPCODE", Text)
    cust_cname = Column("CUST_CNAME", Text)
    cust_corp_emboss_name = Column("CUST_CORP_EMBOSS_NAME", Text)
    cust_cphone = Column("CUST_CPHONE", Text)
    cust_creg_date = Column("CUST_CREG_DATE", Text)
    cust_creg_nbr = Column("CUST_CREG_NBR", Text)
    cust_cbusiness = Column("CUST_CBUSINESS", Text)
    cust_cbank = Column("CUST_CBANK", Text)
    cust_cbank_acct = Column("CUST_CBANK_ACCT", Text)
    cust_ccontact = Column("CUST_CCONTACT", Text)
    cust_collector = Column("CUST_COLLECTOR", Text)
    cust_memo = Column("CUST_MEMO", Text)
    cust_crlimit = Column("CUST_CRLIMIT", Text)
    cust_avail_credit = Column("CUST_AVAIL_CREDIT", Text)
    cust_cash_limit = Column("CUST_CASH_LIMIT", Text)
    cust_avail_cash = Column("CUST_AVAIL_CASH", Text)
    cust_temp_crlimit = Column("CUST_TEMP_CRLIMIT", Text)
    cust_perm_crlimit = Column("CUST_PERM_CRLIMIT", Text)
    cust_date_temp_crlimit_eff = Column("CUST_DATE_TEMP_CRLIMIT_EFF", Text)
    cust_date_temp_crlimit_exp = Column("CUST_DATE_TEMP_CRLIMIT_EXP", Text)
    cust_curr_code = Column("CUST_CURR_CODE", Text)
    cust_addr3 = Column("CUST_ADDR3", Text)
    cust_addr4 = Column("CUST_ADDR4", Text)
    cust_maddr3 = Column("CUST_MADDR3", Text)
    cust_maddr4 = Column("CUST_MADDR4", Text)
    cust_email_addr = Column("CUST_EMAIL_ADDR", Text)
    cust_mobile_phone = Column("CUST_MOBILE_PHONE", Text)
    cust_addr_code = Column("CUST_ADDR_CODE", Text)
    cust_file_buffer = Column("CUST_FILE_BUFFER", Text)
    cust_instl_limit = Column("CUST_INSTL_LIMIT", Text)
    cust_avail_instl = Column("CUST_AVAIL_INSTL", Text)
    cust_stdaln_instl = Column("CUST_STDALN_INSTL", Text)
    cust_avail_stdaln_ins = Column("CUST_AVAIL_STDALN_INS", Text)
    cust_node_id = Column("CUST_NODE_ID", Text)
    cust_family_size = Column("CUST_FAMILY_SIZE", Text)
    cust_spouse_name = Column("CUST_SPOUSE_NAME", Text)
    cust_tax_id = Column("CUST_TAX_ID", Text)
    cust_din = Column("CUST_DIN", Text)
    cust_title = Column("CUST_TITLE", Text)
    cust_cr_card1 = Column("CUST_CR_CARD1", Text)
    cust_cr_card_lmt1 = Column("CUST_CR_CARD_LMT1", Text)
    cust_cr_card2 = Column("CUST_CR_CARD2", Text)
    cust_cr_card_lmt2 = Column("CUST_CR_CARD_LMT2", Text)
    cust_cif = Column("CUST_CIF", Text)
    cust_home_ownshp = Column("CUST_HOME_OWNSHP", Text)
    cust_date_maint = Column("CUST_DATE_MAINT", Text)
    cust_user_maint = Column("CUST_USER_MAINT", Text)
    cust_time_maint = Column("CUST_TIME_MAINT", Text)
    cust_file_date = Column("CUST_FILE_DATE", Text)


class Document(Base):
    """Uploaded supporting document (KTP/KK/NPWP/Cover Buku Tabungan) per result.

    One row per uploaded file. ``ocr_json`` holds the structured OCR output from
    the LLM, tied to ``result_id``.
    """
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(
        UUID(as_uuid=True), ForeignKey("results.id", ondelete="CASCADE"), nullable=False
    )
    doc_type = Column(String(50), nullable=False)  # ktp | kk | npwp | cover_buku_tabungan
    filename = Column(String(255))
    object_path = Column(String(500))  # object name within the documents bucket
    mime_type = Column(String(100))
    status = Column(String(20), nullable=False, default="pending")  # pending|processing|done|failed
    ocr_json = Column(JSONB)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)


class QcStatusRequest(Base):
    """A QC-proposed AI-Status change for a result, pending SPQ Head approval.

    One row per result (``result_id`` is unique): re-submitting by QC upserts the
    same row and resets it to ``pending``. When SPQ Head approves, the Results list
    endpoint displays ``requested_status`` instead of the computed AI status.
    """
    __tablename__ = "qc_status_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Vonis HUMAN untuk tiket ini: PASS (Qualified) | FAIL (Not Qualified) | PENDING.
    # Nilainya sejajar dengan AI Status, tetapi BERDIRI SENDIRI — sejak aturan Manual
    # Status diluruskan, vonis human tidak lagi menimpa kolom AI Status.
    requested_status = Column(String(10), nullable=False)  # PASS | FAIL | PENDING
    reason = Column(Text, nullable=False)
    requested_by_username = Column(String(100))
    requested_by_role = Column(String(20))
    requested_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # Tiered review (QC -> Team Leader QC -> SPQ Head): TL QC's intermediate check.
    tl_qc_status = Column(String(20), nullable=False, default="pending")  # pending|approved|rejected
    tl_qc_username = Column(String(100))
    tl_qc_reviewed_at = Column(DateTime)
    approval_status = Column(String(20), nullable=False, default="pending")  # pending|approved|rejected (SPQ Head, final)
    reviewed_by_username = Column(String(100))
    reviewed_at = Column(DateTime)
    # Reviewer notes, attributed per tier: TL QC's comment (on approve/reject/escalate)
    # and SPQ Head's comment (on approve/reject). Mandatory on a reject, else optional.
    tl_qc_comment = Column(Text)
    review_comment = Column(Text)
    # Bagaimana vonis ini masuk: 'qc' (usulan QC, melewati hierarki QC -> TL QC ->
    # SPQ Head) atau ditetapkan LANGSUNG oleh reviewer yang tidak butuh approval —
    # 'tl_direct' (Team Leader QC) / 'spq_direct' (SPQ Head). Baris direct dibuat
    # sudah final (tl_qc_status='approved'). Mencerminkan kolom yang sama pada
    # ErrorCodeAppeal. Default 'qc' mempertahankan baris lama.
    origin = Column(String(20), nullable=False, default="qc")


class ErrorCodeAppeal(Base):
    """A QC-proposed appeal ("banding") on a single Error Code row, pending SPQ
    Head approval.

    Append-only history: each submission inserts a new row so an error code can
    be appealed repeatedly (the latest row per ``(result_id, error_code,
    item_code)`` is the authoritative status). Only scorecard-sourced rows
    (``item_code`` ``SC_CL_*``) are appealable. When SPQ Head approves, the
    linked scorecard item is flipped ``BELUM_SESUAI`` -> ``SESUAI`` at read time
    (non-destructive), which removes the derived error row and lifts the score.
    """
    __tablename__ = "error_code_appeals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Identity of the appealed Error Code row. Wide enough for the Cashline
    # "B02/B03/B05" fallback code (see migration 0013).
    error_code = Column(String(20), nullable=False)
    item_code = Column(String(50), nullable=False)  # SC_CL_*
    # Snapshot of the AI-generated row (display-only, not editable by QC).
    ai_sumber = Column(String(100))
    ai_risk_base = Column(String(10))
    ai_details_error = Column(Text)
    ai_reason = Column(Text)
    ai_evidence = Column(Text)
    ai_ticket_id = Column(String(100))
    # QC-submitted change (free text).
    qc_reason = Column(Text, nullable=False)
    qc_evidence = Column(Text)
    qc_ticket_id = Column(String(100))
    # Card Holder Verification (B17) appeals only: QC-corrected reference /
    # extracted values (nullable for scorecard SC_CL_* appeals).
    qc_reference_value = Column(Text)
    qc_extracted_value = Column(Text)
    # QC-proposed replacement Error Code (Manual Check form "New Error Code");
    # nullable — empty means the QC did not propose a different code.
    qc_new_error_code = Column(String(20))
    # QC-edited Risk Base override (Manual Check / Add Error Code form). Nullable —
    # empty means "use the master-catalog default for the error code". When set it
    # wins over the catalog value in the built Error Code table.
    qc_risk_base = Column(String(10))
    # Banding intent: 'remove' (approve drops the error + lifts score, current
    # behaviour), 'change' (approve relabels the code to qc_new_error_code;
    # deduction kept if the new code is deduction-bearing, else zeroed), or 'add'
    # (QC proposes a NEW error code — approve attaches it to an existing evaluation
    # item/field and deducts, except source 'others' which is display-only).
    appeal_kind = Column(String(10), nullable=False, default="remove")
    # For appeal_kind='add' only: which source the added error belongs to, since a
    # master-catalog code does not itself imply a source. One of
    # 'scorecard'|'cashline_data'|'card_holder'|'others' (NULL for remove/change).
    add_source = Column(String(20))
    # How this banding entered the system: 'qc' (QC-submitted, flows through the
    # tiered QC -> Team Leader QC -> SPQ Head review) or a DIRECT edit by a reviewer
    # who needs no hierarchy — 'tl_direct' (Team Leader QC) / 'spq_direct' (SPQ Head).
    # A direct row is created already finalized (tl_qc_status='approved'), so it
    # applies immediately. Default 'qc' preserves existing rows.
    origin = Column(String(20), nullable=False, default="qc")
    # Workflow.
    requested_by_username = Column(String(100))
    requested_at = Column(DateTime, server_default=func.now())
    # Tiered review (QC -> Team Leader QC -> SPQ Head): TL QC's intermediate check.
    tl_qc_status = Column(String(20), nullable=False, default="pending")  # pending|approved|rejected
    tl_qc_username = Column(String(100))
    tl_qc_reviewed_at = Column(DateTime)
    approval_status = Column(String(20), nullable=False, default="pending")  # pending|approved|rejected (SPQ Head, final)
    reviewed_by_username = Column(String(100))
    reviewed_at = Column(DateTime)
    # Reviewer notes, attributed per tier: TL QC's comment (on approve/reject/escalate)
    # and SPQ Head's comment (on approve/reject). Mandatory on a reject, else optional.
    tl_qc_comment = Column(Text)
    review_comment = Column(Text)


class QcAssignment(Base):
    """A ticket assigned to a QC by a Team Leader QC (manual assignment for the QC
    division). One ticket -> one QC (``ticket_id`` unique, reassignable). A QC is
    scoped to only their assigned tickets for Results / Statistics / appeals.

    ``ticket_id`` = the customer-id prefix of a result's source filenames (same
    value as ``tms_cashline.result_id`` and the Results list ``id``).
    """
    __tablename__ = "qc_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(100), nullable=False, unique=True, index=True)
    qc_username = Column(String(100), nullable=False, index=True)
    assigned_by_username = Column(String(100))
    assigned_at = Column(DateTime, server_default=func.now())


class QcStatusEvent(Base):
    """Jejak audit APPEND-ONLY setiap perubahan Manual Status (vonis human).

    ``QcStatusRequest`` menyimpan KEADAAN TERKINI saja (satu baris per tiket, ditimpa
    tiap perubahan), jadi tanpa tabel ini tidak ada cara mengetahui vonis sebelumnya,
    siapa mengubahnya, atau apa komentar reviewer yang sudah tertimpa. Satu baris di
    sini = satu kejadian; tidak pernah di-update atau dihapus.

    Sengaja tidak dipakai untuk MENGHITUNG apa pun — Manual Status yang berlaku tetap
    dibaca dari ``QcStatusRequest``. Tabel ini murni untuk ditampilkan.
    """
    __tablename__ = "qc_status_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # usul        -> QC mengajukan (harus lewat hierarki)
    # konfirmasi  -> vonis PERTAMA dari QC yang sama dengan AI Status (final tanpa hierarki)
    # set_langsung-> TL QC / SPQ Head menetapkan sendiri (final saat itu juga)
    # tl_approve | tl_reject | tl_escalate -> keputusan Team Leader QC
    # spq_approve | spq_reject             -> keputusan SPQ Head
    event = Column(String(20), nullable=False)
    actor_username = Column(String(100))
    actor_role = Column(String(20))
    # Vonis yang diusulkan/ditetapkan saat kejadian ini (PASS | FAIL | PENDING).
    requested_status = Column(String(10))
    # Manual Status EFEKTIF sebelum & sesudah kejadian (NULL = belum ada vonis final).
    status_before = Column(String(10))
    status_after = Column(String(10))
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class QcManualCheck(Base):
    """Append-only audit trail of "ticket sudah dicek manual oleh QC" approvals.

    One row per approval EVENT, keyed by ``result_id`` (the check is per-ticket,
    not per error-code row). The latest row per ``result_id`` is the authoritative
    state; earlier rows are kept so a re-check after new evidence stays visible.
    Follows the ``ErrorCodeAppeal`` append-only pattern rather than the
    upsert/destructive ``QcStatusRequest`` one, precisely so the trail survives.

    This is deliberately NOT an AI-Status change: approving here asserts only that
    a human QC reviewed the ticket, and never alters the score or the AI verdict.
    """
    __tablename__ = "qc_manual_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checked_by_username = Column(String(100), nullable=False)
    checked_by_role = Column(String(20))
    note = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class QcDatabase(Base):
    """One row per uploaded QC-database XLSX (Upload Database QC — SPQ Head / Admin).
    Mirrors ``SalesDatabase``: only the newest upload is active."""
    __tablename__ = "qc_databases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    object_path = Column(String(500), nullable=False)
    mime_type = Column(String(100))
    is_active = Column(Boolean, default=True)
    uploaded_by_username = Column(String(100))
    uploaded_by_role = Column(String(20))
    created_at = Column(DateTime, server_default=func.now())


class StatsSnapshot(Base):
    """A precomputed daily snapshot of the Statistics dashboard payload.

    The Statistics aggregation (per-agent / per-campaign / hierarchy error rates)
    is expensive — it scans every ``done`` result's evaluation JSON. Since the
    dashboard is refreshed daily, the full payload is computed at most once per
    WIB calendar day and cached here (see ``crud.get_or_build_stats_snapshot``).
    """
    __tablename__ = "stats_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(String(10), unique=True, nullable=False)  # WIB "YYYY-MM-DD"
    payload = Column(JSONB, nullable=False)
    computed_at = Column(DateTime, server_default=func.now())


class SalesDatabase(Base):
    """An uploaded Sales database (XLSX) file.

    Only the newest upload is active; uploading a new one flips every previous
    row to inactive (see ``crud.create_sales_database``). The raw file is stored
    in the ``sales-database`` MinIO bucket at ``object_path``.
    """
    __tablename__ = "sales_databases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    object_path = Column(String(500), nullable=False)  # object name within the sales-database bucket
    mime_type = Column(String(100))
    is_active = Column(Boolean, default=True)
    uploaded_by_username = Column(String(100))  # who uploaded (Sales Agent)
    uploaded_by_role = Column(String(20))
    created_at = Column(DateTime, server_default=func.now())


class ReprocessJob(Base):
    """Satu perintah "Reprocess All Ticket" (menu Upload Data, khusus Admin).

    Satu job memuat beberapa campaign; per unique ticket id di dalamnya dibuat satu
    ``ReprocessJobItem``. Job-nya sendiri hanya menyimpan perintahnya (campaign apa,
    siapa yang menjalankan, kapan) — kemajuannya dihitung dari status item-item-nya,
    supaya tidak ada dua sumber kebenaran yang bisa berbeda saat worker berjalan
    paralel.

    ``status``: ``running`` | ``done`` | ``cancelled``. ``cancelled`` diminta lewat
    API dan dibaca oleh setiap item SEBELUM memanggil LLM — item yang sudah telanjur
    diproses tetap diselesaikan (menghentikannya di tengah jalan hanya membuang
    biaya panggilan LLM yang sudah dibayar).
    """
    __tablename__ = "reprocess_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaigns = Column(JSONB, nullable=False)  # daftar nama campaign yang dipilih
    # ``campaign``: job massal dari menu "Reprocess All Ticket" (seluruh tiket pada
    # campaign yang dipilih). ``ticket``: satu tiket dari tombol Reprocess di menu
    # Results. Keduanya memakai item & task yang sama; penandanya dipakai supaya
    # layar Reprocess All Ticket tidak menempel pada job satu-tiket milik orang lain.
    scope = Column(String(20), nullable=False, default="campaign")
    status = Column(String(20), nullable=False, default="running")
    total_tickets = Column(Integer, nullable=False, default=0)
    created_by_username = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime)


class ReprocessJobItem(Base):
    """Satu unique ticket id di dalam sebuah ``ReprocessJob``.

    ``old_result_ids`` dibekukan saat job dibuat, BUKAN dicari ulang saat
    penghapusan: hanya row yang memang sudah ada pada saat perintah diberikan yang
    boleh dihapus. Tanpa itu, upload yang masuk di tengah job (mis. lewat webhook)
    ikut terhapus hanya karena ticket id-nya kebetulan sama.

    ``status``: ``pending`` | ``processing`` | ``done`` | ``failed`` | ``skipped``.
    ``failed`` berarti tiket LAMA dipertahankan apa adanya dan row baru yang gagal
    dibuang — sebuah tiket tidak pernah berakhir tanpa hasil sama sekali.
    """
    __tablename__ = "reprocess_job_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(
        UUID(as_uuid=True), ForeignKey("reprocess_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ticket_id = Column(String(100), nullable=False, index=True)
    campaign = Column(String(100))
    old_result_ids = Column(JSONB, nullable=False)  # daftar UUID (string) row lama
    # Row lama TERBARU milik ticket ini — dari sinilah transkrip PDF disalin ke row
    # baru. Selalu termasuk di dalam ``old_result_ids``.
    source_result_id = Column(UUID(as_uuid=True))
    # Row baru hasil reproses — inilah satu-satunya row yang tersisa untuk ticket
    # itu setelah item berstatus ``done``. Dikosongkan lagi pada item ``failed``,
    # karena row barunya memang dibuang dan yang berlaku kembali adalah row lama.
    new_result_id = Column(UUID(as_uuid=True))
    status = Column(String(20), nullable=False, default="pending")
    deleted_old = Column(Integer, nullable=False, default=0)
    error_message = Column(Text)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class AppSetting(Base):
    """Kebijakan tingkat aplikasi yang boleh diubah saat berjalan (key/value).

    Dipakai untuk sakelar yang dulunya konstanta di kode dan karena itu butuh edit
    file + restart container untuk diubah. Baris pertamanya ``doc_sla_enabled``
    (kebijakan tenggat H+2 dokumen pendukung, sakelarnya ada di menu Results dan
    hanya role ``admin`` yang boleh mengubahnya).

    ``value`` sengaja TEXT, bukan boolean: tabel ini generik dan sakelar berikutnya
    belum tentu bertipe boolean. Pembacanya yang mengurus konversi.
    """

    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by_username = Column(String(100))
