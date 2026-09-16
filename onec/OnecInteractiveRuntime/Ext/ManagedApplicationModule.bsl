&После("ПриНачалеРаботыСистемы")
Процедура OnecInteractiveRuntime_ПриНачалеРаботыСистемы()
	ИдентификаторПродуктаRuntime = "onec-interactive-runtime";
	ВерсияАртефактаRuntime = "0.1.3";
	ВерсияПротоколаRuntime = "2";
	ПродолжатьЦикл = Ложь;
	С = 1; // @runtime-extension-service-breakpoint
	Если ПродолжатьЦикл Тогда
		RuntimeKernelServer.Запустить();
	КонецЕсли;
КонецПроцедуры
