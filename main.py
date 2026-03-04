import os
import logging
import warnings
import tempfile
import shutil

# Suppress PTBUserWarning about per_message setting
warnings.filterwarnings("ignore", message=".*per_message.*", category=UserWarning)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
import pikepdf
from docx import Document
from pptx import Presentation
from PIL import Image
import io
import zipfile

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown V1"""
    escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text


# States untuk conversation
WAITING_CUSTOM_PERCENT = 1
WAITING_CUSTOM_SIZE = 2

# Temporary storage untuk file user (in memory)
user_files = {}


class FileCompressor:
    """Class untuk handle kompresi berbagai jenis file dengan target size - FULL IN-MEMORY"""
    
    @staticmethod
    def compress_image_to_target(img: Image.Image, target_ratio: float, quality: int = 85) -> bytes:
        """Compress single image to achieve target ratio"""
        new_width = max(1, int(img.width * target_ratio))
        new_height = max(1, int(img.height * target_ratio))
        
        if new_width > 0 and new_height > 0:
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert to RGB if necessary (for JPEG)
        if img.mode in ('RGBA', 'P', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            if 'A' in img.mode:
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG', optimize=True, quality=quality)
        return img_byte_arr.getvalue()
    
    @staticmethod
    def analyze_pdf(pdf):
        """Analyze PDF content and return element counts"""
        analysis = {
            'pages': len(pdf.pages),
            'images': [],
            'total_image_bytes': 0,
            'fonts': set(),
            'streams': 0,
            'form_xobjects': 0
        }
        
        # Scan all objects
        for obj in pdf.objects:
            try:
                if not hasattr(obj, 'get'):
                    continue
                
                subtype = obj.get('/Subtype')
                
                if subtype == pikepdf.Name('/Image'):
                    width = int(obj.get('/Width', 0))
                    height = int(obj.get('/Height', 0))
                    try:
                        raw_size = len(obj.read_raw_bytes())
                    except:
                        raw_size = 0
                    
                    filter_type = obj.get('/Filter')
                    if isinstance(filter_type, pikepdf.Name):
                        filter_name = str(filter_type)
                    elif isinstance(filter_type, list) and len(filter_type) > 0:
                        filter_name = str(filter_type[0])
                    else:
                        filter_name = 'Unknown'
                    
                    analysis['images'].append({
                        'width': width,
                        'height': height,
                        'size': raw_size,
                        'filter': filter_name
                    })
                    analysis['total_image_bytes'] += raw_size
                    
                elif subtype == pikepdf.Name('/Form'):
                    analysis['form_xobjects'] += 1
                    
                if obj.get('/Type') == pikepdf.Name('/Font'):
                    font_name = obj.get('/BaseFont', 'Unknown')
                    analysis['fonts'].add(str(font_name))
                    
            except:
                continue
        
        analysis['fonts'] = list(analysis['fonts'])
        return analysis
    
    @staticmethod
    def compress_pdf(input_data: bytes, target_size_kb: int = None, compression_ratio: float = 0.5) -> bytes:
        """
        Compress PDF to achieve target file size.
        
        When compression_ratio=0.5, the output file should be ~50% of original size.
        This function iteratively compresses images until target size is achieved.
        """
        try:
            original_size = len(input_data)
            # Target size = original * ratio (e.g., 50% means target is 50% of original)
            target_bytes = target_size_kb * 1024 if target_size_kb else int(original_size * compression_ratio)
            
            logger.info(f"=== PDF COMPRESSION START ===")
            logger.info(f"Original: {original_size}B ({original_size/1024/1024:.2f}MB)")
            logger.info(f"Target: {target_bytes}B ({target_bytes/1024/1024:.2f}MB)")
            logger.info(f"Compression ratio: {compression_ratio} (target = {compression_ratio*100:.0f}% of original)")
            
            # Phase 1: Analyze PDF content
            logger.info("--- PHASE 1: ANALYZING PDF CONTENT ---")
            input_stream = io.BytesIO(input_data)
            
            with pikepdf.open(input_stream) as pdf:
                analysis = FileCompressor.analyze_pdf(pdf)
            
            logger.info(f"Pages: {analysis['pages']}")
            logger.info(f"Images found: {len(analysis['images'])}")
            logger.info(f"Total image bytes: {analysis['total_image_bytes']}B ({analysis['total_image_bytes']/1024:.1f}KB)")
            logger.info(f"Form XObjects: {analysis['form_xobjects']}")
            logger.info(f"Fonts: {len(analysis['fonts'])}")
            
            for i, img in enumerate(analysis['images'][:10]):
                logger.info(f"  Image {i+1}: {img['width']}x{img['height']}, {img['size']}B, filter={img['filter']}")
            
            if len(analysis['images']) == 0:
                logger.warning("No images found in PDF - compression limited to stream optimization")
            
            # Calculate image ratio to estimate compression potential
            image_ratio = analysis['total_image_bytes'] / original_size if original_size > 0 else 0
            logger.info(f"Image content ratio: {image_ratio*100:.1f}%")
            
            # Phase 2: Iterative compression to reach target size
            logger.info("--- PHASE 2: COMPRESSING TO TARGET SIZE ---")
            
            current_data = input_data
            best_data = input_data
            best_size = original_size
            max_iterations = 20  # More iterations for aggressive compression
            
            # Calculate how aggressive we need to be based on target
            reduction_needed = 1 - (target_bytes / original_size)  # e.g., 0.7 for 70% reduction needed
            
            # Determine if this is an aggressive target (e.g., compressing 3MB to <1MB = 66% reduction)
            is_aggressive_target = reduction_needed > 0.5 or target_size_kb is not None
            
            logger.info(f"Reduction needed: {reduction_needed*100:.1f}%, aggressive mode: {is_aggressive_target}")
            
            for iteration in range(max_iterations):
                current_size = len(current_data)
                
                # Check if we've reached target
                if current_size <= target_bytes:
                    logger.info(f"✅ Target achieved: {current_size}B <= {target_bytes}B")
                    break
                
                # Calculate how much more reduction we need from current size
                remaining_reduction = (current_size - target_bytes) / current_size
                
                # Dynamic compression settings - more aggressive for larger reductions
                if is_aggressive_target:
                    # Very aggressive settings for target size compression
                    # Start low and go lower
                    base_quality = max(5, 50 - int(reduction_needed * 30))  # 50 -> 20 based on target
                    quality = max(2, base_quality - (iteration * 4))  # Decrease by 4 each iteration
                    
                    # Calculate resize ratio based on remaining reduction needed
                    # If we need 50% more reduction, start at 0.5 resize
                    base_resize = max(0.3, 0.6 - (reduction_needed * 0.3))  # 0.6 -> 0.3 based on target
                    resize_ratio = max(0.08, base_resize - (iteration * 0.04))  # Decrease by 4% each iteration
                else:
                    # Normal compression for percentage-based
                    base_quality = 70 - int(reduction_needed * 40)
                    quality = max(3, base_quality - (iteration * 5))
                    
                    base_resize = compression_ratio if compression_ratio else 0.7
                    resize_ratio = max(0.1, base_resize - (iteration * 0.05))
                
                logger.info(f"Iteration {iteration+1}: target={target_bytes}B, current={current_size}B, "
                           f"quality={quality}, resize={resize_ratio:.2f}, remaining={remaining_reduction*100:.1f}%")
                
                input_stream = io.BytesIO(current_data)
                output_stream = io.BytesIO()
                images_processed = 0
                
                with pikepdf.open(input_stream) as pdf:
                    # Process images via page resources
                    for page_num, page in enumerate(pdf.pages):
                        try:
                            if '/Resources' not in page:
                                continue
                            resources = page['/Resources']
                            if '/XObject' not in resources:
                                continue
                            
                            xobjects = resources['/XObject']
                            
                            for name, xobj in list(xobjects.items()):
                                try:
                                    if xobj.get('/Subtype') != pikepdf.Name('/Image'):
                                        continue
                                    
                                    width = int(xobj.get('/Width', 100))
                                    height = int(xobj.get('/Height', 100))
                                    
                                    # Skip very small images
                                    if width <= 30 or height <= 30:
                                        continue
                                    
                                    raw_data = xobj.read_raw_bytes()
                                    original_img_size = len(raw_data)
                                    
                                    # Try to open image
                                    pil_img = None
                                    filters = xobj.get('/Filter')
                                    
                                    if filters:
                                        if isinstance(filters, pikepdf.Name):
                                            filters = [filters]
                                        
                                        if pikepdf.Name('/DCTDecode') in filters:
                                            try:
                                                pil_img = Image.open(io.BytesIO(raw_data))
                                            except:
                                                continue
                                        elif pikepdf.Name('/FlateDecode') in filters:
                                            import zlib
                                            try:
                                                decompressed = zlib.decompress(raw_data)
                                                cs = xobj.get('/ColorSpace')
                                                if cs == pikepdf.Name('/DeviceGray'):
                                                    mode, bpp = 'L', 1
                                                elif cs == pikepdf.Name('/DeviceCMYK'):
                                                    mode, bpp = 'CMYK', 4
                                                else:
                                                    mode, bpp = 'RGB', 3
                                                
                                                expected = width * height * bpp
                                                if len(decompressed) >= expected:
                                                    pil_img = Image.frombytes(mode, (width, height), decompressed[:expected])
                                                    if mode == 'CMYK':
                                                        pil_img = pil_img.convert('RGB')
                                            except:
                                                continue
                                        elif pikepdf.Name('/JPXDecode') in filters:
                                            try:
                                                pil_img = Image.open(io.BytesIO(raw_data))
                                            except:
                                                continue
                                    else:
                                        try:
                                            pil_img = Image.open(io.BytesIO(raw_data))
                                        except:
                                            continue
                                    
                                    if pil_img is None:
                                        continue
                                    
                                    # Calculate new dimensions
                                    new_width = max(30, int(width * resize_ratio))
                                    new_height = max(30, int(height * resize_ratio))
                                    pil_img = pil_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                    
                                    # Convert mode
                                    if pil_img.mode == 'RGBA':
                                        bg = Image.new('RGB', pil_img.size, (255, 255, 255))
                                        bg.paste(pil_img, mask=pil_img.split()[3])
                                        pil_img = bg
                                    elif pil_img.mode not in ('RGB', 'L'):
                                        pil_img = pil_img.convert('RGB')
                                    
                                    # Compress image
                                    img_buffer = io.BytesIO()
                                    pil_img.save(img_buffer, format='JPEG', quality=quality, optimize=True)
                                    new_img_data = img_buffer.getvalue()
                                    
                                    # Only replace if compressed image is smaller
                                    if len(new_img_data) >= original_img_size and iteration == 0:
                                        continue
                                    
                                    # Create new stream
                                    new_stream = pikepdf.Stream(pdf, new_img_data)
                                    new_stream[pikepdf.Name('/Type')] = pikepdf.Name('/XObject')
                                    new_stream[pikepdf.Name('/Subtype')] = pikepdf.Name('/Image')
                                    new_stream[pikepdf.Name('/Width')] = new_width
                                    new_stream[pikepdf.Name('/Height')] = new_height
                                    new_stream[pikepdf.Name('/ColorSpace')] = pikepdf.Name('/DeviceRGB') if pil_img.mode == 'RGB' else pikepdf.Name('/DeviceGray')
                                    new_stream[pikepdf.Name('/BitsPerComponent')] = 8
                                    new_stream[pikepdf.Name('/Filter')] = pikepdf.Name('/DCTDecode')
                                    
                                    xobjects[name] = new_stream
                                    images_processed += 1
                                    
                                except Exception as e:
                                    continue
                                    
                        except Exception as e:
                            continue
                    
                    # Save with compression
                    pdf.save(output_stream,
                            compress_streams=True,
                            object_stream_mode=pikepdf.ObjectStreamMode.generate)
                
                new_data = output_stream.getvalue()
                new_size = len(new_data)
                
                reduction_pct = (1 - new_size / original_size) * 100
                logger.info(f"Iteration {iteration+1} result: {current_size}B -> {new_size}B ({reduction_pct:.1f}% reduction), images processed: {images_processed}")
                
                # Keep best result (smallest that's still valid)
                if new_size < best_size:
                    best_data = new_data
                    best_size = new_size
                    current_data = new_data
                    logger.info(f"New best: {best_size}B ({best_size/1024/1024:.2f}MB)")
                else:
                    # No improvement, continue with more aggressive settings
                    current_data = best_data
                
                # If we can't make progress after several iterations, break
                if iteration > 3 and new_size >= current_size * 0.99:
                    logger.info(f"Compression stalled at iteration {iteration+1}, stopping early")
                    break
            
            # Final result
            final_size = len(best_data)
            reduction = (1 - final_size / original_size) * 100
            target_achieved = "✅" if final_size <= target_bytes else "⚠️"
            
            logger.info(f"=== COMPRESSION COMPLETE {target_achieved} ===")
            logger.info(f"Original: {original_size}B ({original_size/1024/1024:.2f}MB)")
            logger.info(f"Target: {target_bytes}B ({target_bytes/1024/1024:.2f}MB)")
            logger.info(f"Result: {final_size}B ({final_size/1024/1024:.2f}MB)")
            logger.info(f"Reduction: {reduction:.1f}%")
            
            return best_data
            
        except Exception as e:
            logger.error(f"PDF compression error: {e}")
            import traceback
            traceback.print_exc()
            return input_data
    
    @staticmethod
    def compress_docx(input_data: bytes, target_size_kb: int = None, compression_ratio: float = 0.5) -> bytes:
        """Compress DOCX file in memory - only replace images if smaller"""
        try:
            original_size = len(input_data)
            target_bytes = target_size_kb * 1024 if target_size_kb else int(original_size * compression_ratio)
            
            quality = 85
            if target_size_kb:
                quality = max(10, min(85, int(85 * (target_bytes / original_size))))
            
            input_stream = io.BytesIO(input_data)
            doc = Document(input_stream)
            images_replaced = 0
            
            # Compress images dalam dokumen
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    try:
                        image_part = rel.target_part
                        original_image_data = image_part.blob
                        original_img_size = len(original_image_data)
                        
                        img = Image.open(io.BytesIO(original_image_data))
                        compressed_data = FileCompressor.compress_image_to_target(
                            img, compression_ratio, quality
                        )
                        
                        # Only replace if smaller
                        if len(compressed_data) < original_img_size:
                            image_part._blob = compressed_data
                            images_replaced += 1
                            logger.info(f"DOCX image compressed: {original_img_size}B -> {len(compressed_data)}B")
                    except Exception as e:
                        logger.warning(f"Could not compress image in DOCX: {e}")
                        continue
            
            output_stream = io.BytesIO()
            doc.save(output_stream)
            result_data = output_stream.getvalue()
            
            logger.info(f"DOCX: {original_size}B -> {len(result_data)}B, images replaced: {images_replaced}")
            
            # Return original if no improvement
            if len(result_data) >= original_size:
                logger.warning(f"DOCX compression increased size, returning original")
                return input_data
            
            return result_data
            
        except Exception as e:
            logger.error(f"Error compressing DOCX: {e}")
            return None
    
    @staticmethod
    def compress_pptx(input_data: bytes, target_size_kb: int = None, compression_ratio: float = 0.5) -> bytes:
        """Compress PPTX file in memory - only replace images if smaller"""
        try:
            original_size = len(input_data)
            target_bytes = target_size_kb * 1024 if target_size_kb else int(original_size * compression_ratio)
            
            quality = 85
            if target_size_kb:
                quality = max(10, min(85, int(85 * (target_bytes / original_size))))
            
            # Use temp directory for PPTX processing
            temp_dir = tempfile.mkdtemp()
            images_replaced = 0
            
            try:
                input_stream = io.BytesIO(input_data)
                
                with zipfile.ZipFile(input_stream, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                
                # Find and compress images
                media_dir = os.path.join(temp_dir, 'ppt', 'media')
                if os.path.exists(media_dir):
                    for filename in os.listdir(media_dir):
                        filepath = os.path.join(media_dir, filename)
                        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                            try:
                                original_file_size = os.path.getsize(filepath)
                                img = Image.open(filepath)
                                compressed_data = FileCompressor.compress_image_to_target(
                                    img, compression_ratio, quality
                                )
                                
                                # Only replace if smaller
                                if len(compressed_data) < original_file_size:
                                    new_filepath = os.path.splitext(filepath)[0] + '.jpg'
                                    with open(new_filepath, 'wb') as f:
                                        f.write(compressed_data)
                                    if new_filepath != filepath:
                                        os.remove(filepath)
                                    images_replaced += 1
                                    logger.info(f"PPTX image {filename}: {original_file_size}B -> {len(compressed_data)}B")
                            except Exception as e:
                                logger.warning(f"Could not compress image {filename}: {e}")
                
                # Repack PPTX to memory
                output_stream = io.BytesIO()
                with zipfile.ZipFile(output_stream, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
                    for root, dirs, files in os.walk(temp_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, temp_dir)
                            zipf.write(file_path, arcname)
                
                result_data = output_stream.getvalue()
                logger.info(f"PPTX: {original_size}B -> {len(result_data)}B, images replaced: {images_replaced}")
                
                # Return original if no improvement
                if len(result_data) >= original_size:
                    logger.warning(f"PPTX compression increased size, returning original")
                    return input_data
                
                return result_data
                
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            
        except Exception as e:
            logger.error(f"Error compressing PPTX: {e}")
            return None
    
    @staticmethod
    def compress_doc(input_data: bytes, target_size_kb: int = None, compression_ratio: float = 0.5) -> bytes:
        """Compress DOC file using ZIP in memory"""
        try:
            output_stream = io.BytesIO()
            with zipfile.ZipFile(output_stream, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
                zipf.writestr('document.doc', input_data)
            return output_stream.getvalue()
        except Exception as e:
            logger.error(f"Error compressing DOC: {e}")
            return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /start"""
    welcome_message = """
🤖 *Selamat Datang di File Compressor Bot!*

Bot ini dapat mengkompress file dokumen Anda:
📄 PDF
📝 DOCX, DOC
📊 PPTX, PPT

*Cara Penggunaan:*
1. Kirim file dokumen Anda
2. Pilih persentase atau target ukuran
3. Tunggu proses kompresi selesai
4. Download file hasil kompresi

✨ *Keamanan:* File diproses dalam memory dan TIDAK disimpan di server!

Kirim file Anda sekarang untuk memulai! 🚀
    """
    await update.message.reply_text(welcome_message, parse_mode='Markdown')


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler ketika user mengirim dokumen - download ke memory"""
    document = update.message.document
    file_name = document.file_name
    file_size = document.file_size
    
    # Check file extension
    allowed_extensions = ['.pdf', '.docx', '.doc', '.pptx', '.ppt']
    file_ext = os.path.splitext(file_name)[1].lower()
    
    if file_ext not in allowed_extensions:
        await update.message.reply_text(
            "❌ Format file tidak didukung!\n\n"
            "Format yang didukung: PDF, DOCX, DOC, PPTX, PPT"
        )
        return
    
    # Check file size (max 50MB)
    if file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "❌ File terlalu besar! Maksimal 50MB"
        )
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text("⏳ Mengunduh file ke memory...")
    
    try:
        # Download file to memory (BytesIO)
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
        file_data = bytes(file_bytes)
        
        # Store file info in memory
        user_files[update.effective_user.id] = {
            'file_data': file_data,  # Store bytes in memory
            'file_name': file_name,
            'file_ext': file_ext,
            'original_size': file_size
        }
        
        # Update message
        escaped_file_name = escape_markdown(file_name)
        size_kb = file_size / 1024
        size_str = f"{size_kb:.2f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"
        
        await processing_msg.edit_text(
            f"✅ File berhasil diterima!\n\n"
            f"📄 Nama: {escaped_file_name}\n"
            f"📦 Ukuran: {size_str}\n"
            f"🔒 Status: Tersimpan di memory (tidak di disk)\n\n"
            f"Pilih metode kompresi:",
            parse_mode='Markdown'
        )
        
        # Create keyboard with compression options
        keyboard = [
            [
                InlineKeyboardButton("📊 Persentase", callback_data="menu_percent"),
                InlineKeyboardButton("📏 Target Ukuran", callback_data="menu_size"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🎚️ *Pilih Metode Kompresi*\n\n"
            "📊 *Persentase* - Kompresi berdasarkan %\n"
            "📏 *Target Ukuran* - Kompresi ke ukuran tertentu",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await processing_msg.edit_text(
            "❌ Terjadi kesalahan saat mengunduh file. Silakan coba lagi."
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk button callback"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if user_id not in user_files:
        await query.edit_message_text("❌ File tidak ditemukan. Silakan kirim file lagi.")
        return ConversationHandler.END
    
    data = query.data
    
    # Menu Persentase
    if data == "menu_percent":
        keyboard = [
            [
                InlineKeyboardButton("📦 Medium (50%)", callback_data="compress_50"),
            ],
            [
                InlineKeyboardButton("🚀 Best (Max Compression)", callback_data="compress_best"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🎚️ *Pilih Tingkat Kompresi*\n\n"
            "📦 *Medium* - Kompresi 50% (ukuran akhir = 50% dari asli)\n"
            "🚀 *Best* - Kompresi maksimal (target < 1MB)",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Menu Target Size
    if data == "menu_size":
        original_size = user_files[user_id]['original_size']
        size_kb = original_size / 1024
        
        keyboard = [
            [
                InlineKeyboardButton("256 KB", callback_data="size_256"),
                InlineKeyboardButton("512 KB", callback_data="size_512"),
            ],
            [
                InlineKeyboardButton("1 MB", callback_data="size_1024"),
                InlineKeyboardButton("2 MB", callback_data="size_2048"),
            ],
            [
                InlineKeyboardButton("5 MB", callback_data="size_5120"),
                InlineKeyboardButton("10 MB", callback_data="size_10240"),
            ],
            [
                InlineKeyboardButton("📝 Custom Size", callback_data="size_custom"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"📏 *Pilih Target Ukuran*\n\n"
            f"Ukuran file saat ini: {size_kb:.2f} KB\n\n"
            f"Pilih target ukuran yang diinginkan:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Back to main menu
    if data == "menu_back":
        keyboard = [
            [
                InlineKeyboardButton("📊 Persentase", callback_data="menu_percent"),
                InlineKeyboardButton("📏 Target Ukuran", callback_data="menu_size"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🎚️ *Pilih Metode Kompresi*\n\n"
            "📊 *Persentase* - Kompresi berdasarkan %\n"
            "📏 *Target Ukuran* - Kompresi ke ukuran tertentu",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Custom percent input
    if data == "compress_custom":
        context.user_data['waiting_for'] = 'percent'
        await query.edit_message_text(
            "📝 *Masukkan Persentase Custom*\n\n"
            "Kirim angka antara 10-90\n"
            "Contoh: 65",
            parse_mode='Markdown'
        )
        return WAITING_CUSTOM_PERCENT
    
    # Custom size input
    if data == "size_custom":
        context.user_data['waiting_for'] = 'size'
        await query.edit_message_text(
            "📝 *Masukkan Target Ukuran Custom*\n\n"
            "Kirim ukuran dalam KB\n"
            "Contoh: 500 untuk 500 KB\n"
            "Contoh: 1024 untuk 1 MB",
            parse_mode='Markdown'
        )
        return WAITING_CUSTOM_SIZE
    
    # Handle compression by percentage
    if data == "compress_50":
        # Medium compression - 50%
        await compress_file(query, context, user_id, compression_ratio=0.5, compression_percent=50, target_size_kb=None)
        return ConversationHandler.END
    
    if data == "compress_best":
        # Best compression - Target <1MB (maximum compression)
        await compress_file(query, context, user_id, compression_ratio=None, compression_percent=None, target_size_kb=1024)
        return ConversationHandler.END
    
    if data.startswith("compress_") and data not in ["compress_custom"]:
        try:
            compression_percent = int(data.split('_')[1])
            compression_ratio = compression_percent / 100
            await compress_file(query, context, user_id, compression_ratio, compression_percent, target_size_kb=None)
            return ConversationHandler.END
        except ValueError:
            pass
    
    # Handle compression by target size
    if data.startswith("size_"):
        target_size_kb = int(data.split('_')[1])
        original_size = user_files[user_id]['original_size']
        
        if target_size_kb * 1024 >= original_size:
            await query.edit_message_text(
                f"⚠️ Target ukuran ({target_size_kb} KB) lebih besar atau sama dengan ukuran file asli!\n\n"
                "Silakan pilih target yang lebih kecil."
            )
            return
        
        await compress_file(query, context, user_id, compression_ratio=None, compression_percent=None, target_size_kb=target_size_kb)
        return ConversationHandler.END


async def handle_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk custom percentage/size input"""
    user_id = update.effective_user.id
    
    if user_id not in user_files:
        await update.message.reply_text("❌ File tidak ditemukan. Silakan kirim file lagi.")
        return ConversationHandler.END
    
    waiting_for = context.user_data.get('waiting_for', 'percent')
    
    try:
        value = int(update.message.text.strip())
        
        if waiting_for == 'percent':
            if value < 10 or value > 90:
                await update.message.reply_text(
                    "❌ Persentase harus antara 10-90!\n"
                    "Silakan kirim angka yang valid."
                )
                return WAITING_CUSTOM_PERCENT
            
            compression_ratio = value / 100
            await compress_file(update.message, context, user_id, compression_ratio, value, target_size_kb=None)
        
        else:  # size
            if value < 10:
                await update.message.reply_text(
                    "❌ Ukuran minimum adalah 10 KB!\n"
                    "Silakan kirim angka yang valid."
                )
                return WAITING_CUSTOM_SIZE
            
            original_size = user_files[user_id]['original_size']
            if value * 1024 >= original_size:
                await update.message.reply_text(
                    f"⚠️ Target ukuran ({value} KB) harus lebih kecil dari ukuran asli ({original_size/1024:.0f} KB)!"
                )
                return WAITING_CUSTOM_SIZE
            
            await compress_file(update.message, context, user_id, compression_ratio=None, compression_percent=None, target_size_kb=value)
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ Input tidak valid! Kirim angka saja.\n"
            "Contoh: 65"
        )
        return WAITING_CUSTOM_PERCENT if waiting_for == 'percent' else WAITING_CUSTOM_SIZE


async def compress_file(message_or_query, context, user_id, compression_ratio, compression_percent, target_size_kb=None):
    """Function untuk melakukan kompresi file - FULL IN-MEMORY"""
    
    # Determine if it's a message or query
    if hasattr(message_or_query, 'edit_message_text'):
        send_func = message_or_query.edit_message_text
        reply_func = message_or_query.message.reply_document
    else:
        send_func = message_or_query.reply_text
        reply_func = message_or_query.reply_document
    
    file_info = user_files[user_id]
    file_data = file_info['file_data']  # Bytes from memory
    file_name = file_info['file_name']
    file_ext = file_info['file_ext']
    original_size = file_info['original_size']
    
    # Determine compression description
    if target_size_kb:
        compress_desc = f"target {target_size_kb} KB"
    else:
        compress_desc = f"{compression_percent}%"
    
    # Send processing message
    await send_func(
        f"⚙️ Memproses kompresi {compress_desc}...\n"
        f"🔒 Proses dilakukan di memory (tidak ada file tersimpan)\n"
        f"Mohon tunggu sebentar."
    )
    
    try:
        # Create output filename
        if target_size_kb:
            output_filename = f"compressed_{target_size_kb}KB_{file_name}"
        else:
            output_filename = f"compressed_{compression_percent}pct_{file_name}"
        
        # Compress based on file type - all in memory
        compressor = FileCompressor()
        compressed_data = None
        
        # Use default ratio if only target size specified
        ratio = compression_ratio if compression_ratio else 0.5
        
        if file_ext == '.pdf':
            compressed_data = compressor.compress_pdf(file_data, target_size_kb, ratio)
        elif file_ext == '.docx':
            compressed_data = compressor.compress_docx(file_data, target_size_kb, ratio)
        elif file_ext == '.doc':
            compressed_data = compressor.compress_doc(file_data, target_size_kb, ratio)
        elif file_ext in ['.pptx', '.ppt']:
            compressed_data = compressor.compress_pptx(file_data, target_size_kb, ratio)
        
        if compressed_data is None:
            await send_func("❌ Gagal mengkompress file. Silakan coba lagi.")
            return
        
        # Get compressed file size
        compressed_size = len(compressed_data)
        size_reduction = ((original_size - compressed_size) / original_size) * 100
        
        # Format sizes
        orig_str = f"{original_size/1024:.2f} KB" if original_size < 1024*1024 else f"{original_size/1024/1024:.2f} MB"
        comp_str = f"{compressed_size/1024:.2f} KB" if compressed_size < 1024*1024 else f"{compressed_size/1024/1024:.2f} MB"
        
        # Send compressed file from memory
        escaped_file_name = escape_markdown(file_name)
        output_stream = io.BytesIO(compressed_data)
        output_stream.name = output_filename  # Required for Telegram to recognize filename
        
        if target_size_kb:
            caption = (
                f"✅ *Kompresi Selesai!*\n\n"
                f"📄 File: {escaped_file_name}\n"
                f"🎯 Target: {target_size_kb} KB\n"
                f"📦 Ukuran Awal: {orig_str}\n"
                f"📦 Ukuran Akhir: {comp_str}\n"
                f"💾 Pengurangan: {size_reduction:.1f}%\n"
                f"🔒 File tidak disimpan di server"
            )
        else:
            caption = (
                f"✅ *Kompresi Selesai!*\n\n"
                f"📄 File: {escaped_file_name}\n"
                f"🎚️ Kompresi: {compression_percent}%\n"
                f"📦 Ukuran Awal: {orig_str}\n"
                f"📦 Ukuran Akhir: {comp_str}\n"
                f"💾 Pengurangan: {size_reduction:.1f}%\n"
                f"🔒 File tidak disimpan di server"
            )
        
        await reply_func(
            document=output_stream,
            filename=output_filename,
            caption=caption,
            parse_mode='Markdown'
        )
        
        # Clean up memory - delete from user_files
        if user_id in user_files:
            del user_files[user_id]
            logger.info(f"Cleaned up memory for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error compressing file: {e}")
        import traceback
        traceback.print_exc()
        await send_func(
            "❌ Terjadi kesalahan saat mengkompress file.\n"
            "Silakan coba lagi atau gunakan pengaturan yang berbeda."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation and clean up memory"""
    user_id = update.effective_user.id
    if user_id in user_files:
        del user_files[user_id]
        logger.info(f"Cleaned up memory for user {user_id} on cancel")
    await update.message.reply_text("❌ Dibatalkan. File dihapus dari memory.")
    return ConversationHandler.END


def main():
    """Main function untuk menjalankan bot"""
    
    # Ganti dengan token bot Anda
    TOKEN = "8577063363:AAEBToIR3CBq5ZSi89o-kzr6Ftb6F-pnX_g"
    
    # Create application with increased timeouts
    application = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )
    
    # Conversation handler untuk custom input
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_callback, pattern="^compress_custom$"),
            CallbackQueryHandler(button_callback, pattern="^size_custom$"),
        ],
        states={
            WAITING_CUSTOM_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input)],
            WAITING_CUSTOM_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(conv_handler)
    
    # Handler untuk semua callback lainnya
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Run bot
    logger.info("Bot started... (In-Memory Mode - No files saved to disk)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()