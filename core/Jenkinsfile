// Core adalah library — tidak menghasilkan image Docker. Pipeline-nya hanya
// memastikan paket bisa di-install & di-import, lalu memicu build API dan worker.
pipeline {
  agent any
  stages {
    stage('Checkout') {
      steps { checkout scm }
    }
    stage('Install & Import Check') {
      steps {
        sh '''
          python3 -m venv .venv
          . .venv/bin/activate
          pip install --upgrade pip
          pip install -r requirements.txt
          # Impor tiap paket bersama: gagal cepat kalau ada dependensi yang
          # tertinggal atau ada sisa "from api ..." yang lolos review.
          PYTHONPATH=. python -c "import core_config, sales_lookup, db.crud, db.models, compliance.stats_aggregate, compliance.evaluator, compliance.error_codes, services.data_dwh"
        '''
      }
    }
    stage('No Leaking Imports') {
      steps {
        // core tidak boleh bergantung pada repo API maupun worker.
        sh '! grep -rn --include=*.py -E "^[[:space:]]*(from|import)[[:space:]]+(api|worker)\\b" .'
      }
    }
  }
  post {
    success {
      build job: 'telemarketing-qc-api',    wait: false
      build job: 'telemarketing-qc-worker', wait: false
    }
  }
}
