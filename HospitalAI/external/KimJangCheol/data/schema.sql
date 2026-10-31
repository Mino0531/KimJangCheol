PRAGMA foreign_keys=ON;

CREATE TABLE call_events (
	id VARCHAR NOT NULL, 
	call_id VARCHAR NOT NULL, 
	source VARCHAR NOT NULL, 
	event_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source, event_id), 
	FOREIGN KEY(call_id) REFERENCES calls (id)
);

CREATE TABLE call_locations (
	id VARCHAR NOT NULL, 
	call_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(call_id) REFERENCES calls (id)
);

CREATE TABLE call_messages (
	id VARCHAR NOT NULL, 
	call_id VARCHAR NOT NULL, 
	sequence INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (call_id, sequence), 
	FOREIGN KEY(call_id) REFERENCES calls (id)
);

CREATE TABLE calls (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR, 
	caller_key VARCHAR NOT NULL, 
	started_at VARCHAR NOT NULL, 
	call_status VARCHAR NOT NULL, 
	review_status VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE chart_imports (
	id VARCHAR NOT NULL, 
	imported_at VARCHAR NOT NULL, 
	patient_count INTEGER NOT NULL, 
	source_name VARCHAR NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE chart_revisions (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	import_id VARCHAR NOT NULL, 
	imported_at VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	previous JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id), 
	FOREIGN KEY(import_id) REFERENCES chart_imports (id)
);

CREATE TABLE desk_actions (
	id VARCHAR NOT NULL, 
	call_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(call_id) REFERENCES calls (id)
);

CREATE TABLE patient_conditions (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE patient_encounters (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE patient_medications (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE patient_observations (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE patient_prescriptions (
	id VARCHAR NOT NULL, 
	patient_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(patient_id) REFERENCES patients (id)
);

CREATE TABLE patients (
	id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE rag_chunks (
	id VARCHAR NOT NULL, 
	document_id VARCHAR NOT NULL, 
	ordinal INTEGER NOT NULL, 
	text VARCHAR NOT NULL, 
	content_sha256 VARCHAR NOT NULL, 
	embedding_status VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (document_id, ordinal), 
	FOREIGN KEY(document_id) REFERENCES rag_documents (id)
);

CREATE TABLE rag_document_versions (
	id VARCHAR NOT NULL, 
	document_id VARCHAR NOT NULL, 
	import_id VARCHAR NOT NULL, 
	imported_at VARCHAR NOT NULL, 
	snapshot JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (document_id, import_id), 
	FOREIGN KEY(document_id) REFERENCES rag_documents (id), 
	FOREIGN KEY(import_id) REFERENCES rag_imports (id)
);

CREATE TABLE rag_documents (
	id VARCHAR NOT NULL, 
	group_code VARCHAR NOT NULL, 
	category VARCHAR NOT NULL, 
	content VARCHAR NOT NULL, 
	keywords JSON NOT NULL, 
	content_sha256 VARCHAR NOT NULL, 
	current_import_id VARCHAR NOT NULL, 
	source_kind VARCHAR NOT NULL, 
	source_url VARCHAR, 
	review_status VARCHAR NOT NULL, 
	verified_at VARCHAR, 
	retrieval_enabled BOOLEAN NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(current_import_id) REFERENCES rag_imports (id)
);

CREATE TABLE rag_imports (
	id VARCHAR NOT NULL, 
	source_name VARCHAR NOT NULL, 
	imported_at VARCHAR NOT NULL, 
	document_count INTEGER NOT NULL, 
	original_source VARCHAR NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE rag_preparation_policies (
	document_id VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (document_id), 
	FOREIGN KEY(document_id) REFERENCES rag_documents (id)
);

CREATE TABLE rag_vocabulary (
	canonical VARCHAR NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (canonical)
);

CREATE INDEX ix_call_events_call_id ON call_events (call_id);

CREATE INDEX ix_call_locations_call_id ON call_locations (call_id);

CREATE INDEX ix_call_messages_call_id ON call_messages (call_id);

CREATE INDEX ix_calls_call_status ON calls (call_status);

CREATE INDEX ix_calls_caller_key ON calls (caller_key);

CREATE INDEX ix_calls_patient_id ON calls (patient_id);

CREATE INDEX ix_calls_review_status ON calls (review_status);

CREATE INDEX ix_calls_started_at ON calls (started_at);

CREATE INDEX ix_chart_revisions_import_id ON chart_revisions (import_id);

CREATE INDEX ix_chart_revisions_imported_at ON chart_revisions (imported_at);

CREATE INDEX ix_chart_revisions_patient_id ON chart_revisions (patient_id);

CREATE INDEX ix_desk_actions_call_id ON desk_actions (call_id);

CREATE INDEX ix_patient_conditions_patient_id ON patient_conditions (patient_id);

CREATE INDEX ix_patient_encounters_patient_id ON patient_encounters (patient_id);

CREATE INDEX ix_patient_medications_patient_id ON patient_medications (patient_id);

CREATE INDEX ix_patient_observations_patient_id ON patient_observations (patient_id);

CREATE INDEX ix_patient_prescriptions_patient_id ON patient_prescriptions (patient_id);

CREATE INDEX ix_rag_chunks_document_id ON rag_chunks (document_id);

CREATE INDEX ix_rag_document_versions_document_id ON rag_document_versions (document_id);

CREATE INDEX ix_rag_documents_category ON rag_documents (category);

CREATE INDEX ix_rag_documents_group_code ON rag_documents (group_code);

CREATE INDEX ix_rag_documents_review_status ON rag_documents (review_status);
