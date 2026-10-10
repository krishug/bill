from __future__ import annotations

import queue
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from .cloud import VisionConfig, cloud_fallback_needed, extract_with_vision
from .excel_export import export_excel
from .models import InvoiceRecord, parse_decimal, record_from_cloud
from .validation import mark_duplicates, validate_record


class InvoiceApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("批量发票图片识别与 Excel 导出")
        self.root.geometry("1260x820")
        self.root.minsize(1000, 680)
        self.records: list[InvoiceRecord] = []
        self.folder = tk.StringVar()
        self.selected_paths: list[Path] | None = None
        self.output_path = tk.StringVar(value=str(Path.cwd() / "发票识别结果.xlsx"))
        self.status_text = tk.StringVar(value="请选择发票图片文件夹。首次本地识别需要下载中文 OCR 模型。")
        self.progress_text = tk.StringVar(value="尚未开始")
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.cloud_session_id = str(uuid.uuid4())
        self.preview_image: ImageTk.PhotoImage | None = None
        self._build_ui()
        self.root.after(120, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        paths = ttk.LabelFrame(outer, text="文件与任务", padding=10)
        paths.pack(fill="x")
        ttk.Label(paths, text="图片/PDF 文件夹或文件").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.folder).grid(row=0, column=1, sticky="ew", pady=4)
        input_actions = ttk.Frame(paths)
        input_actions.grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Button(input_actions, text="选择文件…", command=self._choose_files).pack(side="left")
        ttk.Button(input_actions, text="选择文件夹…", command=self._choose_folder).pack(side="left", padx=(6, 0))
        ttk.Label(paths, text="Excel 保存位置").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.output_path).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(paths, text="选择路径…", command=self._choose_output).grid(row=1, column=2, padx=(8, 0), pady=4)
        paths.columnconfigure(1, weight=1)

        actions = ttk.Frame(outer, padding=(0, 10))
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="开始本地识别", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.export_button = ttk.Button(actions, text="导出 Excel", command=self._export, state="disabled")
        self.export_button.pack(side="left")
        self.edit_button = ttk.Button(actions, text="人工修正所选记录…", command=self._edit_selected, state="disabled")
        self.edit_button.pack(side="left", padx=(8, 0))
        self.cloud_button = ttk.Button(actions, text="云端补识别（需逐张确认）", command=self._cloud_selected, state="disabled")
        self.cloud_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.progress_text).pack(side="right")
        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.summary = ttk.Label(outer, text="成功 0　待复核 0　失败 0")
        self.summary.pack(anchor="w", pady=(0, 8))

        split = ttk.Panedwindow(outer, orient="vertical")
        split.pack(fill="both", expand=True)
        table_frame = ttk.LabelFrame(split, text="识别结果（双击也可编辑）", padding=6)
        preview_frame = ttk.LabelFrame(split, text="所选图片与 OCR 原始文字", padding=6)
        split.add(table_frame, weight=3)
        split.add(preview_frame, weight=2)

        columns = ("index", "number", "amount", "tax", "total", "seller", "filename", "page", "status", "validation")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "index": "序号", "number": "发票号码", "amount": "金额",
            "tax": "税额", "total": "价税合计金额", "seller": "销售方公司名称",
            "filename": "原始文件名", "page": "PDF页码", "status": "状态", "validation": "校验结果",
        }
        widths = {"index": 48, "number": 150, "amount": 90, "tax": 90, "total": 110, "seller": 200, "filename": 180, "page": 70, "status": 80, "validation": 300}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=55, anchor="w", stretch=column in {"filename", "seller", "validation"})
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected())

        preview = ttk.Frame(preview_frame)
        preview.pack(fill="both", expand=True)
        self.image_label = ttk.Label(preview, text="选择一条记录查看原图", anchor="center", width=56)
        self.image_label.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.raw_text = tk.Text(preview, wrap="word", width=64, height=12, font=("Consolas", 9))
        self.raw_text.pack(side="left", fill="both", expand=True)
        self.raw_text.configure(state="disabled")
        ttk.Label(outer, textvariable=self.status_text, wraplength=1180).pack(anchor="w", pady=(8, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含发票图片的文件夹")
        if selected:
            self.selected_paths = None
            self.folder.set(selected)

    def _choose_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="选择发票图片或 PDF 文件",
            filetypes=[("发票文件", "*.jpg *.jpeg *.png *.pdf"), ("全部文件", "*.*")],
        )
        if selected:
            self.selected_paths = [Path(item) for item in selected]
            self.folder.set(f"已选择 {len(self.selected_paths)} 个文件")

    def _choose_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="选择 Excel 保存位置", defaultextension=".xlsx", filetypes=[("Excel 工作簿", "*.xlsx")],
            initialfile="发票识别结果.xlsx",
        )
        if selected:
            self.output_path.set(selected)

    def _start(self) -> None:
        from .batch import discover_input_files

        folder = Path(self.folder.get().strip()) if self.selected_paths is None else None
        paths = self.selected_paths or (discover_input_files(folder) if folder and folder.is_dir() else [])
        paths = [item for item in paths if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".pdf"}]
        if not paths:
            messagebox.showinfo("没有发票文件", "所选位置中没有 JPG、JPEG、PNG 或 PDF 文件。")
            return
        if self.records and not messagebox.askyesno("开始新批次", "新批次将替换当前界面中的结果。是否继续？"):
            return
        self.records.clear()
        self._refresh_table()
        self.progress.stop()
        self.progress.configure(mode="indeterminate", maximum=len(paths), value=0)
        self.progress.start(12)
        self.progress_text.set(f"0 / {len(paths)} 个文件")
        self.status_text.set("本地 CPU OCR 处理中；单张失败不会中断其余图片。")
        self.cancel_event.clear()
        self.cloud_session_id = str(uuid.uuid4())
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.worker = threading.Thread(target=self._run_worker, args=(paths,), daemon=True)
        self.worker.start()

    def _run_worker(self, paths: list[Path]) -> None:
        try:
            from .batch import BatchProcessor

            records = BatchProcessor().process_paths(
                paths,
                cancel_event=self.cancel_event,
                progress=lambda current, total, record: self.events.put(("progress", current, total, record)),
            )
            self.events.put(("done", records, self.cancel_event.is_set()))
        except Exception as exc:
            self.events.put(("fatal", f"{type(exc).__name__}: {exc}"))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_text.set("已请求取消；当前图片处理完成后将停止。")
        self.cancel_button.configure(state="disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, current, total, record = event
                    if str(self.progress.cget("mode")) == "indeterminate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress.configure(maximum=total, value=current)
                    page_label = f" 第 {record.source_page} 页" if record.source_page else ""
                    self.progress_text.set(f"{current} / {total}　最近：{record.original_filename}{page_label}")
                    self.summary.configure(text=f"处理中　最近一张：{record.status}")
                elif event[0] == "done":
                    _, self.records, cancelled = event
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._refresh_table()
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.export_button.configure(state="normal" if self.records else "disabled")
                    self.progress_text.set("已取消" if cancelled else "处理完成")
                    self.status_text.set("批次已停止，已完成记录可导出。" if cancelled else "本地批次处理完成；请复核标记为“待复核”的记录。")
                elif event[0] == "fatal":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    messagebox.showerror("批次处理失败", event[1])
                elif event[0] == "cloud_done":
                    _, index, fields = event
                    self._apply_cloud_result(index, fields)
                elif event[0] == "cloud_error":
                    messagebox.showerror("云端识别失败", event[1])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_events)

    def _refresh_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for record in self.records:
            self.tree.insert("", "end", iid=str(record.index), values=(
                record.index, record.invoice_number or "", _money(record.amount), _money(record.tax),
                _money(record.total), record.seller_name or "", record.original_filename,
                ",".join(str(page) for page in record.source_pages), record.status,
                "；".join(record.validation_result),
            ))
        counts = {name: sum(item.status == name for item in self.records) for name in ("成功", "待复核", "失败")}
        self.summary.configure(text=f"成功 {counts['成功']}　待复核 {counts['待复核']}　失败 {counts['失败']}")
        self.export_button.configure(state="normal" if self.records else "disabled")
        self._selection_changed()

    def _selected_record(self) -> InvoiceRecord | None:
        selected = self.tree.selection()
        if not selected:
            return None
        index = int(selected[0])
        return next((item for item in self.records if item.index == index), None)

    def _selection_changed(self, _event: object = None) -> None:
        record = self._selected_record()
        state = "normal" if record else "disabled"
        self.edit_button.configure(state=state)
        allow_cloud = bool(
            record and record.recognition_source.startswith("local") and cloud_fallback_needed(record)
        )
        self.cloud_button.configure(state="normal" if allow_cloud else "disabled")
        if not record:
            self.image_label.configure(image="", text="选择一条记录查看原图")
            self.preview_image = None
            self._set_raw_text("")
            return
        try:
            if Path(record.file_path).suffix.lower() == ".pdf":
                import pymupdf

                from io import BytesIO

                with pymupdf.open(record.file_path) as document:
                    page_index = max(0, (record.source_page or 1) - 1)
                    pixmap = document.load_page(page_index).get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
            else:
                image = ImageOps.exif_transpose(Image.open(record.file_path)).convert("RGB")
            image.thumbnail((540, 340))
            self.preview_image = ImageTk.PhotoImage(image)
            self.image_label.configure(image=self.preview_image, text="")
        except Exception as exc:
            self.image_label.configure(image="", text=f"无法预览图片\n{exc}")
            self.preview_image = None
        self._set_raw_text(record.raw_text or record.error or "没有可显示的 OCR 原始文字。")

    def _set_raw_text(self, text: str) -> None:
        self.raw_text.configure(state="normal")
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", text)
        self.raw_text.configure(state="disabled")

    def _edit_selected(self) -> None:
        record = self._selected_record()
        if not record:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"人工修正：{record.original_filename}")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = [
            ("发票号码", "invoice_number", record.invoice_number or ""),
            ("金额", "amount", _money(record.amount)),
            ("税额", "tax", _money(record.tax)),
            ("价税合计", "total", _money(record.total)),
            ("销售方公司名称", "seller_name", record.seller_name or ""),
        ]
        variables: dict[str, tk.StringVar] = {}
        for row, (label, key, value) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=6)
            variable = tk.StringVar(value=value)
            variables[key] = variable
            ttk.Entry(dialog, textvariable=variable, width=46).grid(row=row, column=1, sticky="ew", padx=12, pady=6)

        def save() -> None:
            for key in ("amount", "tax", "total"):
                raw_value = variables[key].get().strip()
                if raw_value and parse_decimal(raw_value) is None:
                    messagebox.showerror("金额格式错误", f"“{raw_value}”不是有效金额。", parent=dialog)
                    return
            record.invoice_number = variables["invoice_number"].get().strip() or None
            record.amount = parse_decimal(variables["amount"].get())
            record.tax = parse_decimal(variables["tax"].get())
            record.total = parse_decimal(variables["total"].get())
            record.seller_name = variables["seller_name"].get().strip() or None
            record.recognition_source = "manual edit"
            record.error = ""
            validate_record(record)
            mark_duplicates(self.records)
            dialog.destroy()
            self._refresh_table()
            self.tree.selection_set(str(record.index))
            self.status_text.set("已保存人工修正；原始 OCR 文本仍保留。")

        buttons = ttk.Frame(dialog)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", padx=12, pady=12)
        ttk.Button(buttons, text="保存修正", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=8)
        dialog.columnconfigure(1, weight=1)

    def _cloud_selected(self) -> None:
        record = self._selected_record()
        if not record:
            return
        if not record.recognition_source.startswith("local") or not cloud_fallback_needed(record):
            return
        config = VisionConfig.from_environment()
        if not config:
            messagebox.showinfo(
                "云端服务未配置",
                "云端功能默认关闭。请设置 OPENCODE_GO_API_KEY，或设置\n"
                "INVOICE_VISION_API_KEY 及服务 endpoint/model；本地功能不受影响。",
            )
            return
        confirmed = messagebox.askyesno(
            "确认上传这张图片",
            f"将仅上传所选文件/页面到已配置的视觉模型服务：\n\n{record.original_filename}"
            f"{f'（第 {record.source_page} 页）' if record.source_page else ''}\n\n是否确认上传？",
        )
        if not confirmed:
            return
        self.cloud_button.configure(state="disabled")
        self.status_text.set(f"正在对 {record.original_filename} 执行已确认的云端识别…")
        threading.Thread(
            target=self._run_cloud,
            args=(record.index, record.file_path, config, self.cloud_session_id, record.source_page),
            daemon=True,
        ).start()

    def _run_cloud(
        self, index: int, path: str, config: VisionConfig, session_id: str, page_number: int | None
    ) -> None:
        try:
            fields = extract_with_vision(path, config, session_id=session_id, page_number=page_number)
            self.events.put(("cloud_done", index, fields))
        except Exception as exc:
            self.events.put(("cloud_error", f"{type(exc).__name__}: {exc}"))

    def _apply_cloud_result(self, index: int, fields: dict) -> None:
        record = next((item for item in self.records if item.index == index), None)
        if not record:
            return
        cloud_record = record_from_cloud(index, record.file_path, fields)
        record.invoice_number = cloud_record.invoice_number
        record.invoice_code = cloud_record.invoice_code
        record.invoice_date = cloud_record.invoice_date
        record.amount = cloud_record.amount
        record.tax = cloud_record.tax
        record.total = cloud_record.total
        record.seller_name = cloud_record.seller_name
        record.recognition_source = cloud_record.recognition_source
        record.error = ""
        validate_record(record)
        mark_duplicates(self.records)
        self._refresh_table()
        self.tree.selection_set(str(record.index))
        self.status_text.set("已完成用户确认的云端补识别，并重新执行本地字段校验。")

    def _export(self) -> None:
        if not self.records:
            return
        destination = self.output_path.get().strip()
        if not destination:
            self._choose_output()
            destination = self.output_path.get().strip()
        if not destination:
            return
        try:
            path = export_excel(self.records, destination)
            self.status_text.set(f"已导出 Excel：{path}")
            messagebox.showinfo("导出完成", f"已保存到：\n{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", f"{type(exc).__name__}: {exc}")


def _money(value: object) -> str:
    return f"{value:.2f}" if value is not None else ""


def launch() -> None:
    root = tk.Tk()
    InvoiceApp(root)
    root.mainloop()
