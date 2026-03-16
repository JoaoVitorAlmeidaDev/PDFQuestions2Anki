from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import os
import json
import uuid
from parser.image_extractor import PDFImageExtractor
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='anki_import')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # Limite de 200MB para vários PDFs

# Garante que a pasta de uploads existe
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "Arquivos muito grandes. O limite total é de 200MB."}), 413

@app.route('/')
def index():
    return render_template('index.html')

import re
import unicodedata

def sanitize_name(name: str) -> str:
    name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower().strip()
    name = re.sub(r'[\s\W_]+', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name if name else "sem_nome"


@app.route('/check_folder', methods=['POST'])
def check_folder():
    disciplina = request.json.get('disciplina', '')
    if not disciplina:
        return jsonify({"exists": False})
        
    safe_disciplina = sanitize_name(disciplina)
    target_path = os.path.join(app.root_path, "anki_import", safe_disciplina)
    
    # Se a pasta existe e tem arquivos dentro
    if os.path.exists(target_path) and os.listdir(target_path):
        return jsonify({"exists": True})
        
    return jsonify({"exists": False})

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'pdf' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    
    files = request.files.getlist('pdf')
    disciplina = request.form.get('disciplina', 'Desconhecida')
    overwrite = request.form.get('overwrite') == 'true'
    
    if overwrite:
        import shutil
        safe_disciplina = sanitize_name(disciplina)
        target_path = os.path.join(app.root_path, "anki_import", safe_disciplina)
        if os.path.exists(target_path):
            shutil.rmtree(target_path, ignore_errors=True)
            
    try:
        header_height = int(request.form.get('header_margin', 70))
    except ValueError:
        header_height = 70
        
    try:
        footer_height = int(request.form.get('footer_margin', 70))
    except ValueError:
        footer_height = 70
    
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "Nenhum arquivo válido selecionado"}), 400
    
    import time
    saved_filepaths = []
    
    # Salvar todos os arquivos IMEDIATAMENTE antes de começar o stream.
    # Se deixarmos para salvar dentro do generator (next), o Flask/Werkzeug
    # já terá fechado os arquivos temporários da requisição (gerando "read of closed file").
    for file in files:
        if file and file.filename.lower().endswith('.pdf'):
            timestamp = int(time.time() * 1000)
            filename = f"{timestamp}_{secure_filename(file.filename)}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            saved_filepaths.append(filepath)
            
    def generate():
        safe_disciplina = sanitize_name(disciplina)
        target_dir = os.path.join(app.root_path, "anki_import", safe_disciplina)
        csv_path = os.path.join(target_dir, "cards.csv")
        
        all_existing_questions = []
        
        if not overwrite and os.path.exists(csv_path):
            try:
                import csv
                with open(csv_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if len(row) >= 3:
                            # Reconstruct q_dict from CSV row if possible
                            import re
                            front_img = re.search(r'src="([^"]+)"', row[0])
                            back_img = re.search(r'src="([^"]+)"', row[1])
                            if front_img and back_img:
                                all_existing_questions.append({
                                    "front_image": front_img.group(1),
                                    "back_image": back_img.group(1),
                                    "tags": row[2].split(' ')
                                })
            except:
                pass

        current_session_questions = []
        try:
            for filepath in saved_filepaths:
                try:
                    extractor = PDFImageExtractor(filepath, disciplina=disciplina)
                    for question in extractor.extract_question_images(header_height=header_height, footer_height=footer_height):
                        q_dict = question.model_dump()
                        
                        # Adiciona caminhos para exibição no frontend
                        q_dict["front_url"] = f"/anki_import/{safe_disciplina}/images/{q_dict['front_image']}"
                        q_dict["back_url"] = f"/anki_import/{safe_disciplina}/images/{q_dict['back_image']}"
                        
                        current_session_questions.append(q_dict)
                        yield json.dumps(q_dict) + "\n"
                        
                finally:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except:
                            pass
            
            # Ao final de todos os arquivos da sessão, salvar o CSV consolidado
            if current_session_questions or all_existing_questions:
                final_questions = all_existing_questions + current_session_questions
                os.makedirs(target_dir, exist_ok=True)
                
                import csv
                with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.writer(f)
                    # Não colocamos header conforme o padrão do Anki, mas o usuário pediu Frente,Verso,Tags
                    # Se ele quiser o header litetal como primeira linha:
                    # writer.writerow(["Frente", "Verso", "Tags"]) 
                    for q in final_questions:
                        row = [
                            f'<img src="{q["front_image"]}">',
                            f'<img src="{q["back_image"]}">',
                            " ".join(q.get("tags", []))
                        ]
                        writer.writerow(row)
                    
        except Exception as e:
            yield json.dumps({"error": f"Erro no processamento: {str(e)}"}) + "\n"

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')

if __name__ == '__main__':
    app.run(debug=True)
