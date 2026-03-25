pi-mounting-rig.dxf: pi-mounting-rig.ps
	@if command -v pstoedit >/dev/null 2>&1; then \
		pstoedit -psarg "-r600x600" -dt -f "dxf_s:-mm -ctl" $< $@; \
	else \
		echo "Warning: pstoedit not found. Install with: brew install pstoedit"; \
	fi

%.dxf: %.ps
	@if command -v pstoedit >/dev/null 2>&1; then \
		pstoedit -psarg "-r600x600" -dt -f "dxf_s:-mm -ctl" $< $@; \
	else \
		echo "Warning: pstoedit not found. Install with: brew install pstoedit"; \
	fi

%.dxf: %.pdf
	@if command -v pstoedit >/dev/null 2>&1; then \
		pstoedit -psarg "-r600x600" -dt -f "dxf_s:-mm -ctl" $< $@; \
	else \
		echo "Warning: pstoedit not found. Install with: brew install pstoedit"; \
	fi

clean:
	rm -f pi-mounting-rig.dxf
