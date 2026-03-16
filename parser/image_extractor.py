import fitz
import os
import re
import uuid
from typing import List, Dict, Any, Generator
from models.question import Question

class PDFImageExtractor:
    def __init__(self, pdf_path: str, disciplina: str = "Desconhecida"):
        self.pdf_path = pdf_path
        self.disciplina = disciplina
        self.pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
        self.safe_pdf_name = self._sanitize_name(self.pdf_basename)
        
        # Gera nomes de pastas seguros (sem espaços ou caracteres especiais)
        safe_disciplina = self._sanitize_name(disciplina)
        
        # Define o diretório de saída organizado apenas por disciplina
        self.relative_output_path = safe_disciplina
        self.output_dir = os.path.join("anki_import", self.relative_output_path, "images")
        # os.makedirs(self.output_dir, exist_ok=True) -> Removido do init para permitir check externo

    def _sanitize_name(self, name: str) -> str:
        """Converte uma string para um formato seguro para pastas (ex: 'Direito Penal' -> 'direito_penal')."""
        import unicodedata
        # Remove acentos
        name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
        name = name.lower().strip()
        # Substitui espaços e caracteres não alfanuméricos por underscores
        name = re.sub(r'[\s\W_]+', '_', name)
        # Remove underscores duplicados ou nas extremidades
        name = re.sub(r'_+', '_', name).strip('_')
        return name if name else "sem_nome"

    def _is_header_color(self, color_int: int) -> bool:
        """
        Heuristic to check if a color is in the blue/purple range used for headers.
        """
        if color_int == 0:
            return False
            
        r = (color_int >> 16) & 255
        g = (color_int >> 8) & 255
        b = color_int & 255
        
        return b > 80 and b > g + 20

    def extract_question_images(self, header_height: int = 70, footer_height: int = 70):
        """
        Extracts question images in two distinct phases.
        """
        import gc
        all_jobs_list: List[Dict[str, Any]] = []
        try:
            if not os.path.exists(self.pdf_path):
                raise FileNotFoundError(f"Arquivo não encontrado: {self.pdf_path}")
            
            os.makedirs(self.output_dir, exist_ok=True)
            
            with open(self.pdf_path, "rb") as pdf_file_handle:
                pdf_data_bytes = pdf_file_handle.read()
                
            pdf_doc = fitz.open(stream=pdf_data_bytes, filetype="pdf")
            try:
                starts_found: List[Dict[str, Any]] = []
                ends_found: List[Dict[str, Any]] = []
                
                # 1.1 Find Markers with Heuristic Scoring
                for current_pg_num in range(len(pdf_doc)):
                    current_pg = pdf_doc[current_pg_num]
                    crop_area = fitz.Rect(0, header_height, current_pg.rect.width, current_pg.rect.height - footer_height)
                    text_meta = current_pg.get_text("dict", clip=crop_area)
                    
                    lines_on_page = []
                    for b_node in text_meta.get("blocks", []):
                        if "lines" in b_node:
                            for l_node in b_node["lines"]:
                                line_text = "".join(s["text"] for s in l_node["spans"]).strip()
                                if not line_text: continue
                                
                                line_bbox = list(l_node["bbox"])
                                main_color = 0
                                for s in l_node["spans"]:
                                    if s["text"].strip():
                                        main_color = int(s.get("color", 0))
                                        break
                                
                                lines_on_page.append({
                                    "text": line_text,
                                    "bbox": line_bbox,
                                    "color": main_color,
                                    "page": current_pg_num,
                                    "spans": l_node["spans"]
                                })
                    
                    lines_on_page.sort(key=lambda x: (x["bbox"][1], x["bbox"][0]))

                    for i, line_meta in enumerate(lines_on_page):
                        txt = line_meta["text"]
                        score = 0

                        # ── 1. Numeração: linha começa com número + ponto ou parêntese → +4
                        # Regex: ^\s*\d{1,3}[.)] — permite um ou mais espaços antes do número
                        has_number = bool(re.match(r'^\s*\d{1,3}[.\)]', txt))
                        if has_number:
                            score += 4

                        bancas_pattern = r'(CESPE|CEBRASPE|FGV|FCC|VUNESP|IBFC|IADES|AOCP|FUNDATEC|QUADRIX|CONSULPLAN|IDECAN|ESAF|CESGRANRIO)'
                        
                        # ── 2. Banca / Ano na mesma linha → +2
                        # Exemplos: (CESPE – EMAP – 2018), (FGV – PC AM – 2022)
                        has_banca_inline = (
                            bool(re.search(r'\(\s*.*\d{4}\s*\)', txt)) or
                            bool(re.search(r'^\s*\d{1,3}\s*[.\)]\s*(?:-\s*)?\(?\s*' + bancas_pattern, txt, re.IGNORECASE)) or
                            any(lbl in txt.upper() for lbl in ["ANO:", "BANCA:", "ÓRGÃO:", "PROVA:"])
                        )
                        if has_banca_inline:
                            score += 2

                        # ── 2b. Banca sozinha na linha (sem número) → +3
                        # Para PDFs onde a questão começa direto com a banca: "(FGV – MPE-AL – 2018)"
                        is_banca_only_line = (
                            not has_number and
                            bool(re.search(r'^\s*\(?\s*' + bancas_pattern, txt, re.IGNORECASE)) and
                            bool(re.search(r'\d{4}\s*\)?', txt)) and
                            len(txt) < 200
                        )
                        if is_banca_only_line:
                            score += 3

                        # ── Exclusão: linha claramente é item de gabarito / resultado isolado → -20
                        is_answer_text = any(k in txt.upper() for k in ["LETRA ", "CORRETA", "ERRADA", "GABARITO:", "GABARITO "])
                        if is_answer_text and len(txt) < 50:
                            score = -20

                        # ── 3. Heurística de Contexto (para linhas numeradas ou banca-only) ──
                        if (has_number or is_banca_only_line) and score >= 0:
                            alt_found = 0
                            has_gabarito_nearby = False
                            has_comentarios_nearby = False

                            for j in range(i + 1, min(i + 15, len(lines_on_page))):
                                next_txt = lines_on_page[j]["text"]
                                if re.match(r'^[a-e][\.\)]', next_txt.lower()):
                                    alt_found += 1
                                # Novo Regex flexível para achar Gabarito + Resposta (ex: Gabarito: A, O gabarito é B)
                                gabarito_pattern = r'gabarito\s*(?:(?:da\s*quest\w+\s*é|é|oficial\s*é|correto\s*é|:)?\s*(?:letra\s*)?\(?[A-Ea-e]\)?|:?\s*(?:correto|correta|errado|errada)|é\s*(?:correto|errado))'
                                if re.search(gabarito_pattern, next_txt.lower()):
                                    has_gabarito_nearby = True
                                if any(k in next_txt for k in ["Comentário", "Comentários", "COMENTÁRIO", "COMENTÁRIOS", "Comentßrios"]):
                                    has_comentarios_nearby = True

                            if alt_found >= 2:
                                score += 4  # presença de alternativas a–e → +4
                            if has_gabarito_nearby:
                                score += 2  # presença de "Gabarito" → +2
                            if has_comentarios_nearby:
                                score += 1  # presença de "Comentários" → +1

                        # ── 3b. Bônus Extra: banca na linha seguinte (enunciado em 2 linhas) ──
                        if has_number and score < 5:
                            for j in range(i + 1, min(i + 4, len(lines_on_page))):
                                next_txt = lines_on_page[j]["text"]
                                if any(k in next_txt.upper() for k in ["LETRA ", "CORRETA", "ERRADA"]) and len(next_txt) < 30:
                                    break
                                if re.search(r'\(\s*.*\d{4}\s*\)', next_txt) or any(lbl in next_txt.upper() for lbl in ["ANO:", "BANCA:", "ÓRGÃO:", "PROVA:"]):
                                    score += 2
                                    break

                        line_meta["score"] = score
                        is_h_color = self._is_header_color(line_meta["color"])
                        has_meta_labels = any(lbl in txt.upper() for lbl in ["ANO:", "BANCA:", "ÓRGÃO:", "PROVA:"])

                        # Limiar: score ≥ 5 indica início de questão
                        is_question_start = (score >= 5) or (is_h_color and has_meta_labels)
                        
                        if is_question_start:
                            # Deduplicação por número de questão
                            num_match = re.search(r'^\s*(\d{1,3})[.\)]', txt)
                            q_num = int(num_match.group(1)) if num_match else None
                            
                            is_duplicate = False
                            if starts_found:
                                # A) Duplicado por posição
                                last_start = starts_found[-1]
                                if last_start["page"] == current_pg_num and abs(line_meta["bbox"][1] - last_start["bbox"][1]) < 10:
                                    is_duplicate = True
                                
                                # B) Duplicado por Conteúdo (crucial para PDF com seções repetidas)
                                if not is_duplicate and q_num:
                                    for idx, prev in enumerate(starts_found):
                                        prev_num_match = re.search(r'^\s*(\d{1,3})[.\)]', prev["text"])
                                        prev_num = int(prev_num_match.group(1)) if prev_num_match else None
                                        
                                        if prev_num == q_num:
                                            # Preferir a versão com MAIOR score.
                                            # Score já inclui bônus por ter marcadores próximos,
                                            # então a versão comentada normalmente ganha.
                                            # Só substitui se o novo score for estritamente maior.
                                            if score > prev.get("score", 0):
                                                starts_found[idx] = line_meta
                                            is_duplicate = True
                                            break
                            
                            if not is_duplicate:
                                starts_found.append(line_meta)
                        
                        # Marcadores (Gabarito/Comentário/Comentários)
                        gabarito_pattern = r'gabarito\s*(?:(?:da\s*quest\w+\s*é|é|oficial\s*é|correto\s*é|:)?\s*(?:letra\s*)?\(?[A-Ea-e]\)?|:?\s*(?:correto|correta|errado|errada)|é\s*(?:correto|errado))'
                        if re.search(gabarito_pattern, txt.lower()) or any(k in txt for k in ["Comentário", "Comentários", "COMENTÁRIO", "COMENTÁRIOS", "Comentßrios"]):
                            ends_found.append(line_meta)
                        
                starts_found.sort(key=lambda x: (x["page"], x["bbox"][1]))
                print(f"DEBUG: Scanned PDF. Total starts={len(starts_found)}, Total ends={len(ends_found)}")
                if ends_found:
                    print(f"DEBUG: First few ends: {[(e['page']+1, round(e['bbox'][1],1), e['text'][:15]) for e in ends_found[:10]]}")
                
                if starts_found:
                    print(f"DEBUG: First start Pg {starts_found[0]['page']+1} y={starts_found[0]['bbox'][1]:.1f}: {starts_found[0]['text'][:30]!r}")
                    print(f"DEBUG: Last start Pg {starts_found[-1]['page']+1} y={starts_found[-1]['bbox'][1]:.1f}: {starts_found[-1]['text'][:30]!r}")

                # 1.2 Pair Markers and Extract Raw Pixmap Bytes
                for s_index, st_item in enumerate(starts_found):
                    nxt_st_item = starts_found[s_index+1] if s_index+1 < len(starts_found) else None
                    
                    if nxt_st_item:
                        matched_end_item = {
                            "page": nxt_st_item["page"],
                            "bbox": [0, nxt_st_item["bbox"][1] - 5, 0, nxt_st_item["bbox"][1] - 2],
                            "text": "Next Question Boundary"
                        }
                    else:
                        last_pg = len(pdf_doc) - 1
                        matched_end_item = {
                            "page": min(last_pg, st_item["page"] + 3), # Limite máximo: 3 páginas p/ a última questão
                            "bbox": [0, pdf_doc[min(last_pg, st_item["page"] + 3)].rect.height - footer_height - 10, 0, pdf_doc[min(last_pg, st_item["page"] + 3)].rect.height - footer_height],
                            "text": "End of PDF Boundary / Max Pages"
                        }

                    # REMOVIDA A HEURISTICA ANTI-LIXO ANTERIOR AQUI, movida para depois da busca de gabarito

                    current_job = {
                        "filename": f"{self.safe_pdf_name}_q_{st_item['page']+1}_{s_index+1}.png",
                        "pixmaps_raw": [],
                        "markers": {"comentario": None, "gabarito": None}
                    }

                    found_count = 0
                    if st_item["page"] == 111: # Pg 112
                        print(f"DEBUG: Checking Q{s_index+1} Pg 112 range. NextQ: Pg {nxt_st_item['page']+1 if nxt_st_item else 'EOF'}")
                    
                    found_gabarito_end = None

                    for ed_item in ends_found:
                        is_after = (ed_item["page"] > st_item["page"]) or (ed_item["page"] == st_item["page"] and ed_item["bbox"][1] > st_item["bbox"][1])
                        is_before = True
                        if nxt_st_item:
                            is_before = (ed_item["page"] < nxt_st_item["page"]) or (ed_item["page"] == nxt_st_item["page"] and ed_item["bbox"][1] < nxt_st_item["bbox"][1])
                        
                        if is_after and is_before:
                            found_count += 1
                            txt_ed = ed_item["text"].lower()
                            m_coord = (ed_item["page"], ed_item["bbox"][1])
                            if "coment" in txt_ed:
                                if current_job["markers"]["comentario"] is None or m_coord < current_job["markers"]["comentario"]:
                                    current_job["markers"]["comentario"] = m_coord
                            else:
                                if current_job["markers"]["gabarito"] is None or m_coord < current_job["markers"]["gabarito"]:
                                    current_job["markers"]["gabarito"] = m_coord
                                    # Se achou gabarito com resposta exata, este já é o fim da questão.
                                    found_gabarito_end = ed_item
                    
                    if found_gabarito_end:
                        # Substitui o "matched_end_item" genérico pelo limite exato do gabarito.
                        matched_end_item = {
                            "page": found_gabarito_end["page"],
                            "bbox": [0, found_gabarito_end["bbox"][3], 0, found_gabarito_end["bbox"][3]],
                            "text": "Exact Gabarito Boundary"
                        }
                    
                    # NOVA HEURISTICA: Tem que haver Gabarito, e ele deve estar em até 3 páginas do enunciado.
                    # Além disso, o laço de "ends_found" já garante que procuramos o gabarito APENAS ANTES da próxima questão.
                    # Se não achou na janela (st_item e nxt_st_item), é porque veio outra questão antes do gabarito ou acabou as 3 páginas.
                    gabarito_pg = current_job["markers"]["gabarito"][0] if current_job["markers"]["gabarito"] else None
                    if gabarito_pg is None or (gabarito_pg - st_item["page"]) > 3:
                        print(f"DEBUG: Q{s_index+1} (Pg {st_item['page']+1}) SKIPPED. Unanswered list or gabarito too far (max 3 pgs).")
                        continue

                    if current_job["markers"]["comentario"] is None or current_job["markers"]["gabarito"] is None:
                        # Em vez de pular a questão, apenas a registramos (e o corte do verso será aproximado)
                        print(f"DEBUG: Q{s_index+1} (Pg {st_item['page']+1} y={st_item['bbox'][1]:.1f}) found NO MARKERS. Considering next Q start as boundary.")
                    else:
                        print(f"DEBUG: Q{s_index+1} (Pg {st_item['page']+1}) OK. Markers: {current_job['markers']}")

                    for p_range in range(st_item["page"], matched_end_item["page"] + 1):
                        pg_to_crop = pdf_doc[p_range]
                        y0_coord = st_item["bbox"][1] if p_range == st_item["page"] else header_height
                        if p_range == st_item["page"]:
                            y0_coord = max(header_height, y0_coord - 5)
                        
                        # O fim do recorte deve ser o final da página ou o início da próxima questão
                        # garantindo que o comentário não seja cortado.
                        y1_coord = (matched_end_item["bbox"][3] + 10) if p_range == matched_end_item["page"] else pg_to_crop.rect.height - footer_height
                        
                        final_rect = fitz.Rect(0, y0_coord, pg_to_crop.rect.width, y1_coord)
                        final_rect = final_rect & pg_to_crop.rect
                        if not final_rect.is_empty:
                            px_result = pg_to_crop.get_pixmap(clip=final_rect, matrix=fitz.Matrix(2, 2))
                            current_job["pixmaps_raw"].append({
                                "bytes": px_result.tobytes(),
                                "height": px_result.height,
                                "width": px_result.width,
                                "page_num": p_range,
                                "y0_rect": final_rect.y0,
                                "y1_rect": final_rect.y1
                            })
                            px_result = None
                        pg_to_crop = None
                    
                    if current_job["pixmaps_raw"]:
                        all_jobs_list.append(current_job)
            finally:
                if 'pdf_doc' in locals() and pdf_doc:
                    pdf_doc.close()
                    pdf_doc = None
                gc.collect()

            # PHASE 2: PROCESSING AND YIELDING
            from PIL import Image
            import io

            for active_job_data in all_jobs_list:
                question_images = []
                total_h_px = 0
                max_w_px = 0
                
                parts_meta = []
                for part_data in active_job_data["pixmaps_raw"]:
                    img_part = Image.open(io.BytesIO(part_data["bytes"]))
                    question_images.append(img_part)
                    
                    parts_meta.append({
                        "page_num": part_data["page_num"],
                        "y0_offset_px": total_h_px,
                        "pdf_y0": part_data["y0_rect"],
                        "pdf_y1": part_data["y1_rect"],
                        "px_h": part_data["height"]
                    })
                    
                    total_h_px += part_data["height"]
                    max_w_px = max(max_w_px, part_data["width"])
                
                if not question_images: continue
                
                final_combined_img = Image.new('RGB', (max_w_px, total_h_px), (255, 255, 255))
                y_cursor = 0
                for img_part in question_images:
                    final_combined_img.paste(img_part, (0, y_cursor))
                    y_cursor += img_part.height
                    img_part.close()
                
                y_split_px = total_h_px 
                
                def pdf_to_px(pdf_y, part_meta):
                    # Relative position in PDF rect -> Scale to Pixmap height
                    rel_y = (pdf_y - part_meta["pdf_y0"]) / (part_meta["pdf_y1"] - part_meta["pdf_y0"])
                    return part_meta["y0_offset_px"] + (rel_y * part_meta["px_h"])

                m_com = active_job_data["markers"]["comentario"]
                m_gab = active_job_data["markers"]["gabarito"]
                target_marker = m_com if m_com else m_gab
                
                if target_marker:
                    p_num, y_pdf = target_marker
                    found_p = False
                    for p_meta in parts_meta:
                        if p_meta["page_num"] == p_num:
                            y_split_px = pdf_to_px(y_pdf, p_meta)
                            found_p = True
                            break
                    if not found_p:
                        # Fallback case
                        y_split_px = total_h_px - min(80, total_h_px // 3)
                    
                    # Clamp: ensure split doesn't consume whole image
                    y_split_px = max(20, min(int(y_split_px) - 5, total_h_px - 20))
                else:
                    # No marker found, we fallback to a bit before the end of the image 
                    # so at least there is a "back" image to serve as a spacer/fallback
                    y_split_px = max(total_h_px // 2, total_h_px - 80)
                q_id = str(uuid.uuid4())[:8]

                # SAVE FRONT
                front_img = final_combined_img.crop((0, 0, max_w_px, y_split_px))
                front_filename = f"{q_id}_front.png"
                front_path = os.path.join(self.output_dir, front_filename)
                front_img.save(front_path)
                front_img.close()

                # SAVE BACK
                back_img = final_combined_img.crop((0, y_split_px, max_w_px, total_h_px))
                back_filename = f"{q_id}_back.png"
                back_path = os.path.join(self.output_dir, back_filename)
                back_img.save(back_path)
                back_img.close()

                final_combined_img = None
                tags = [self._sanitize_name(self.disciplina)]
                
                yield Question(
                    id=q_id, 
                    front_image=front_filename, 
                    back_image=back_filename,
                    tags=tags
                )

        except Exception as e:
            print(f"Extraction Error Critical: {e}")
            raise

if __name__ == "__main__":
    pass
